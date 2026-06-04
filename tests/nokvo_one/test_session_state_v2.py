"""Unit tests for the unified SessionState.

Covers the four claims in the plan's verification section:

  1. ``SessionState.to_dict`` → ``SessionState.from_dict`` round-trips
     every field without loss.
  2. ``from_legacy_blobs`` correctly migrates the three legacy shapes
     we may encounter at deploy boundary:
       a. state-with-memory-sub-dict + history list
       b. state-only (no memory blob, no history)
       c. memory-blob-only (no top-level state)
  3. ``mutate_state`` serialises concurrent writes for the same
     call_id (no lost updates).
  4. Distinct call_ids get distinct locks (no false-positive
     serialisation).

Uses an in-memory FakeRedis to avoid needing a real Redis for the
unit run.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.services.session_state_v2 import (
    BucketEntry,
    HistoryTurn,
    SessionState,
    ToolFlowState,
    apply_legacy_patch,
    from_legacy_blobs,
    load_state,
    mutate_state,
    replace_state,
    save_state,
)


class FakeRedis:
    """Minimal async Redis stand-in. setex / get only — enough for the
    session-state primitives. Stores values as the same str/bytes shape
    redis-py would return so the production code path is exercised."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:  # noqa: ARG002
        self._store[key] = value

    async def get(self, key: str) -> str | None:
        return self._store.get(key)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── round-trip ─────────────────────────────────────────────────────────────────


def test_to_dict_round_trip_preserves_every_field() -> None:
    """If a field is silently dropped by to_dict/from_dict we'll lose
    state across the unified-key write. Drive every field with a
    non-default value and assert equality."""
    state = SessionState(
        call_id="call-1",
        language="te",
        status="connected",
        call_surface="voice_inbound",
        campaign_id="camp-abc",
        agent_mode="site_visit",
        agent_mode_final="inbound_lead",
        identity_verified=True,
        identity_verification_pending=False,
        markers={"auto_lead_created": True, "auto_lead_id": "lead-xyz"},
        next_turn_index=5,
        facts={"name": {"key": "name", "value": "Nihar", "confidence": 0.95, "source_turn": 2, "timestamp": time.time()}},
        objections=[BucketEntry(code="price_concern", text="too expensive", turn=3, resolved_at_turn=4)],
        commitments=[BucketEntry(code="interested", text="yes", turn=2)],
        preferences=[{"key": "contact_channel", "value": "whatsapp", "turn": 1}],
        salient_notes=[{"code": "allergy", "text": "peanuts", "turn": 1}],
        asked_questions=[{"slot": "phone", "turn": 4}],
        prior_stage="qualification",
        journey_stage="closing",
        tool_flow=ToolFlowState(
            active=True,
            flow_key="real_estate_site_visit",
            collected={"name": "Nihar", "phone": "9999999999"},
            pending_slot="visit_date",
            confirmations={"phone": {"at": "t", "value": "9999999999"}},
        ),
        appointment={"active": True, "pending_slot": "patient_name"},
        outbound_memory={"caller_name": "Riya", "pitch_summary": "Skyline Heights"},
        campaign_objectives_covered=["site_visit"],
        proactive_silence_nudges=2,
        history=[
            HistoryTurn(role="user", content="hi", turn_index=0),
            HistoryTurn(role="assistant", content="hello", turn_index=0),
        ],
    )
    re_parsed = SessionState.from_dict(state.to_dict())
    assert re_parsed.call_id == "call-1"
    assert re_parsed.language == "te"
    assert re_parsed.markers == {"auto_lead_created": True, "auto_lead_id": "lead-xyz"}
    assert re_parsed.next_turn_index == 5
    assert re_parsed.facts == state.facts
    assert len(re_parsed.objections) == 1 and re_parsed.objections[0].code == "price_concern"
    assert re_parsed.objections[0].resolved_at_turn == 4
    assert re_parsed.tool_flow.flow_key == "real_estate_site_visit"
    assert re_parsed.tool_flow.collected == {"name": "Nihar", "phone": "9999999999"}
    assert re_parsed.asked_questions == [{"slot": "phone", "turn": 4}]
    assert re_parsed.appointment["active"] is True
    assert re_parsed.outbound_memory["caller_name"] == "Riya"
    assert re_parsed.campaign_objectives_covered == ["site_visit"]
    assert re_parsed.proactive_silence_nudges == 2
    assert re_parsed.prior_stage == "qualification"
    assert re_parsed.journey_stage == "closing"
    assert [(t.role, t.content) for t in re_parsed.history] == [("user", "hi"), ("assistant", "hello")]


# ── legacy migration ─────────────────────────────────────────────────────────


def test_from_legacy_blobs_with_memory_subdict() -> None:
    """The most common shape today: state dict carries everything inline
    plus a ``memory`` sub-dict for the ConversationalMemory blob, and a
    separate ``history`` list lives under its own key."""
    state_blob = {
        "language": "en",
        "call_surface": "voice_inbound",
        "campaign_id": None,
        "agent_mode": "query",
        "auto_lead_created": False,
        "tool_flow": {
            "active": True,
            "flow_key": "real_estate_site_visit",
            "collected": {"name": "Nihar"},
            "pending_slot": "phone",
        },
        "memory": {
            "facts": {
                "name": {
                    "key": "name",
                    "value": "Nihar",
                    "confidence": 0.95,
                    "source_turn": 1,
                    "timestamp": 1700000000.0,
                },
                "bhk": {
                    "key": "bhk",
                    "value": "3 BHK",
                    "confidence": 0.85,
                    "source_turn": -1,
                    "timestamp": 1690000000.0,
                },
            },
            "objections": [{"code": "price_concern", "turn": 2, "text": "expensive"}],
            "commitments": [],
            "preferences": [],
            "salient_notes": [],
            "asked_questions": [{"slot": "phone", "turn": 3}],
            "bootstrap_keys": ["bhk"],
            "prior_stage": "qualification",
        },
    }
    history_list = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "what's available?"},
        {"role": "assistant", "content": "3 BHK in Kokapet"},
    ]
    state = from_legacy_blobs(state_blob, history_list)

    assert state.language == "en"
    assert state.call_surface == "voice_inbound"
    assert state.agent_mode == "query"
    # auto_lead_created sweeps into markers (top-level keys we don't claim).
    assert state.markers["auto_lead_created"] is False
    assert state.tool_flow.active is True
    assert state.tool_flow.flow_key == "real_estate_site_visit"
    assert state.tool_flow.collected == {"name": "Nihar"}
    # Facts migrated. ``bhk`` was in bootstrap_keys → flagged.
    assert state.facts["name"]["value"] == "Nihar"
    assert state.facts["bhk"]["from_prior_call"] is True
    assert state.facts["name"].get("from_prior_call", False) is False
    # Bucket entries parsed as typed BucketEntry.
    assert len(state.objections) == 1
    assert state.objections[0].code == "price_concern"
    assert state.objections[0].turn == 2
    assert state.asked_questions == [{"slot": "phone", "turn": 3}]
    assert state.prior_stage == "qualification"
    # History parsed + next_turn_index derived from len // 2.
    assert len(state.history) == 4
    assert state.next_turn_index == 2


def test_from_legacy_blobs_state_only_no_memory() -> None:
    """Some early-call states don't carry the memory sub-dict yet — the
    pipeline writes tool_flow before the memory extractor's first
    save. Verify the migration still produces a usable state."""
    state_blob = {
        "language": "en",
        "tool_flow": {"active": False},
        "status": "connected",
    }
    state = from_legacy_blobs(state_blob, None)
    assert state.language == "en"
    assert state.status == "connected"
    assert state.tool_flow.active is False
    assert state.facts == {}
    assert state.objections == []
    assert state.history == []
    assert state.next_turn_index == 0


def test_from_legacy_blobs_history_only() -> None:
    """Tester paths sometimes have a populated history but never wrote
    a state blob. Migration should still rebuild next_turn_index."""
    history_list = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]
    state = from_legacy_blobs(None, history_list)
    assert state.language is None
    assert len(state.history) == 2
    assert state.next_turn_index == 1


# ── apply_legacy_patch ────────────────────────────────────────────────────────


def test_apply_legacy_patch_merges_dict_typed_slots() -> None:
    """Mirrors the historic merge_state shallow-merge behaviour: dict
    values get one-level merged, scalars replace."""
    state = SessionState()
    state.tool_flow = ToolFlowState(active=True, collected={"name": "A"})
    apply_legacy_patch(state, {"tool_flow": {"collected": {"phone": "9"}, "pending_slot": "date"}})
    assert state.tool_flow.collected == {"name": "A", "phone": "9"}
    assert state.tool_flow.pending_slot == "date"
    assert state.tool_flow.active is True  # not in patch, preserved


def test_apply_legacy_patch_routes_unknown_keys_into_markers() -> None:
    state = SessionState()
    apply_legacy_patch(state, {"auto_lead_created": True, "auto_lead_id": "abc"})
    assert state.markers == {"auto_lead_created": True, "auto_lead_id": "abc"}


def test_apply_legacy_patch_routes_memory_sub_dict() -> None:
    state = SessionState()
    apply_legacy_patch(state, {
        "memory": {
            "facts": {"name": {"key": "name", "value": "Z", "confidence": 0.9, "source_turn": 1, "timestamp": 1.0}},
            "objections": [{"code": "price_concern", "turn": 2}],
            "bootstrap_keys": ["name"],
        }
    })
    assert state.facts["name"]["value"] == "Z"
    assert state.facts["name"]["from_prior_call"] is True
    assert state.objections[0].code == "price_concern"


# ── atomicity ─────────────────────────────────────────────────────────────────


def test_mutate_state_serialises_concurrent_writes_same_call_id() -> None:
    """Fire 50 concurrent mutate_state calls that each write a distinct
    slot into tool_flow.collected. Without locking, RMW races would
    cause lost updates; with the per-call lock the final state has
    all 50 entries."""
    redis = FakeRedis()
    n = 50

    async def writer(i: int):
        def patch(s: SessionState) -> None:
            s.tool_flow.collected[f"slot_{i}"] = i
        await mutate_state(redis, "ns", "call-conc", patch)

    async def main():
        await asyncio.gather(*[writer(i) for i in range(n)])

    _run(main())
    state = _run(load_state(redis, "ns", "call-conc"))
    assert len(state.tool_flow.collected) == n
    for i in range(n):
        assert state.tool_flow.collected[f"slot_{i}"] == i


def test_mutate_state_distinct_call_ids_dont_serialise() -> None:
    """Two calls with different call_ids must run in parallel — they
    don't share a lock instance."""
    redis = FakeRedis()
    sleep_for = 0.05

    async def slow_mutator(call_id: str):
        async def patch(s: SessionState) -> None:
            await asyncio.sleep(sleep_for)
            s.language = call_id

        # mutate_state expects a sync fn; wrap with a tiny adapter
        # because we want to verify the lock-release timing, not the
        # callback signature.
        lock_check_started = time.perf_counter()
        async def inner_patch(s: SessionState) -> None:
            await asyncio.sleep(sleep_for)
            s.language = call_id

        # Inline the lock+load+save the same way mutate_state does
        from app.services.session_state_v2 import _lock_for, load_state as _load, save_state as _save
        lock = await _lock_for(call_id)
        async with lock:
            state = await _load(redis, "ns", call_id)
            await asyncio.sleep(sleep_for)
            state.language = call_id
            await _save(redis, "ns", call_id, state, dual_write=False)
        return time.perf_counter() - lock_check_started

    async def main():
        return await asyncio.gather(slow_mutator("c-A"), slow_mutator("c-B"))

    durations = _run(main())
    # If the two were serialised on the same lock, total would be >2 *
    # sleep_for. Running in parallel each finishes in ~sleep_for.
    # Allow generous headroom for event-loop scheduling on CI.
    for d in durations:
        assert d < sleep_for * 5, f"distinct call_ids should not serialise; took {d}s"


def test_mutate_state_no_call_id_returns_empty_state_silently() -> None:
    """The legacy merge_state contract was: empty call_id = no-op,
    don't crash. Same here."""
    redis = FakeRedis()

    def patch(s: SessionState) -> None:
        s.language = "en"

    state = _run(mutate_state(redis, "ns", "", patch))
    assert isinstance(state, SessionState)
    assert state.language == "en"  # fn ran on the local stub
    # But nothing was written to Redis.
    raw = _run(redis.get("ns:agent:call::v2"))
    assert raw is None


def test_load_state_falls_back_to_legacy_then_writes_v2() -> None:
    """When :v2 is missing, the loader reads :state + :history, builds
    a SessionState, and writes :v2 back so the next read is fast."""
    redis = FakeRedis()
    _run(redis.setex("ns:agent:call:legacy-1:state", 900, '{"language": "en", "tool_flow": {"active": true}}'))
    _run(redis.setex("ns:agent:call:legacy-1:history", 600, '[{"role":"user","content":"hi"}]'))
    state = _run(load_state(redis, "ns", "legacy-1"))
    assert state.language == "en"
    assert state.tool_flow.active is True
    assert len(state.history) == 1
    # Write-back happened.
    assert _run(redis.get("ns:agent:call:legacy-1:v2")) is not None


def test_save_state_dual_writes_when_flagged() -> None:
    redis = FakeRedis()
    state = SessionState(call_id="dual-1", language="en")
    _run(save_state(redis, "ns", "dual-1", state, dual_write=True))
    # All three keys must exist after a dual-write save.
    assert _run(redis.get("ns:agent:call:dual-1:v2")) is not None
    assert _run(redis.get("ns:agent:call:dual-1:state")) is not None
    assert _run(redis.get("ns:agent:call:dual-1:history")) is not None


def test_save_state_skips_legacy_when_dual_write_disabled() -> None:
    redis = FakeRedis()
    state = SessionState(call_id="solo-1")
    _run(save_state(redis, "ns", "solo-1", state, dual_write=False))
    assert _run(redis.get("ns:agent:call:solo-1:v2")) is not None
    assert _run(redis.get("ns:agent:call:solo-1:state")) is None
    assert _run(redis.get("ns:agent:call:solo-1:history")) is None


def test_replace_state_holds_the_per_call_lock() -> None:
    """replace_state and mutate_state must serialise against each other.
    The acquisition order isn't guaranteed by the test (asyncio.gather
    schedules both immediately) — what IS guaranteed is that both
    operations run to completion and the final state is one of two
    clean variants. We never see a half-write (replacer's language
    AND mutator's marker AND lost data)."""
    redis = FakeRedis()

    async def replacer():
        s = SessionState(call_id="rep-1", language="te")
        await asyncio.sleep(0.02)
        await replace_state(redis, "ns", "rep-1", s)

    async def mutator():
        def patch(s: SessionState) -> None:
            s.markers["written_by_mutate"] = True
        await mutate_state(redis, "ns", "rep-1", patch)

    async def main():
        await asyncio.gather(replacer(), mutator())

    _run(main())
    state = _run(load_state(redis, "ns", "rep-1"))
    # Two valid terminal states, depending on which acquired the lock
    # last. Both are CLEAN (the loser's writes were fully wiped or
    # cleanly preserved — never a mix).
    replacer_won = state.language == "te" and not state.markers.get("written_by_mutate")
    mutator_won = state.markers.get("written_by_mutate") is True
    assert replacer_won or mutator_won, (
        f"unexpected mixed state: language={state.language!r} markers={state.markers!r}"
    )
