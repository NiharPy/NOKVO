"""In-call rolling summary (conversational awareness)."""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.services import in_call_summary_service as ics
from app.services.conversational_memory import ConversationalMemory


def _run(coro):
    return asyncio.run(coro)


# ── persistence + render ─────────────────────────────────────────────────────


def test_rolling_summary_round_trips():
    m = ConversationalMemory(rolling_summary="Caller Nihar wants a 3 BHK; objected to price.",
                             summarized_turns=4)
    m2 = ConversationalMemory.from_state_blob(m.to_state_blob())
    assert m2.rolling_summary == m.rolling_summary
    assert m2.summarized_turns == 4


def test_compose_prompt_block_renders_summary():
    m = ConversationalMemory(rolling_summary="Discussed Skyline Heights; caller comparing with a competitor.")
    block = m.compose_prompt_block("en", business_type="real_estate")
    assert "# CONVERSATION SO FAR" in block
    assert "Skyline Heights" in block
    # No summary → no section.
    assert "# CONVERSATION SO FAR" not in ConversationalMemory().compose_prompt_block("en")


# ── fold (mock nano) ─────────────────────────────────────────────────────────


def test_fold_calls_nano_and_returns_summary(monkeypatch):
    from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM

    async def _fake_nano(messages, *, max_tokens=120):
        # echo that it saw the prior + new lines
        joined = messages[-1]["content"]
        return "SUMMARY: " + ("has-prior" if "Running summary so far" in joined else "fresh")

    monkeypatch.setattr(AzureGroundedLLM, "complete_nano", staticmethod(_fake_nano))
    monkeypatch.setattr(settings, "IN_CALL_SUMMARY_ENABLED", True)

    fresh = _run(ics.fold("", [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]))
    assert fresh == "SUMMARY: fresh"
    extended = _run(ics.fold("prior text", [{"role": "user", "content": "more"}]))
    assert extended == "SUMMARY: has-prior"


def test_fold_noop_on_empty_or_disabled(monkeypatch):
    assert _run(ics.fold("keep", [])) == "keep"
    monkeypatch.setattr(settings, "IN_CALL_SUMMARY_ENABLED", False)
    assert _run(ics.fold("keep", [{"role": "user", "content": "x"}])) == "keep"


# ── maybe_update: eviction cadence ───────────────────────────────────────────


class _FakeStore:
    def __init__(self, history):
        self._history = history

    async def get_history(self, tenant_res, call_id):
        return self._history


def test_maybe_update_folds_only_evicted_and_advances_cursor(monkeypatch):
    monkeypatch.setattr(settings, "IN_CALL_SUMMARY_ENABLED", True)
    monkeypatch.setattr(settings, "IN_CALL_SUMMARY_CONDENSE_WINDOW", 6)

    history = [{"role": "user", "content": f"m{i}"} for i in range(10)]  # 10 messages
    monkeypatch.setattr(ics, "settings", settings)

    saved = {}
    mem = ConversationalMemory()

    async def _fake_fold(prior, evicted, *, language="en"):
        return f"{prior}|folded({len(evicted)})".strip("|")

    async def _load(tenant_res, call_id):
        return mem

    async def _save(tenant_res, call_id, memory):
        saved["memory"] = memory

    import app.services.agent_session_store as ass
    import app.services.conversational_memory as cm
    monkeypatch.setattr(ass.AgentSessionStore, "get_history", classmethod(lambda cls, t, c: _ret(history)))
    monkeypatch.setattr(cm, "load_memory", _load)
    monkeypatch.setattr(cm, "save_memory", _save)
    monkeypatch.setattr(ics, "fold", _fake_fold)

    async def _ret(v):
        return v

    _run(ics.maybe_update(object(), "call-1", language="en"))
    # boundary = 10 - 6 = 4 → folds messages [0:4]; cursor advances to 4.
    assert saved["memory"].summarized_turns == 4
    assert "folded(4)" in saved["memory"].rolling_summary
