"""Cost-tracker wiring — the metering chokepoints + post-call attribution.

DB-free: LLM pool / HTTP / Redis / DB are faked. These pin:
  * ``complete_nano`` meters its usage into the ambient sink exactly once
    (the one previously-unmetered LLM path), and is side-effect-free with no
    sink installed;
  * ``_meter_call_llm`` understands BOTH usage shapes (chat-completions and
    the Responses API);
  * ``attribute_post_call_llm`` folds post-call tokens onto the committed
    CallCost row with one guarded atomic UPDATE, and never raises;
  * ``post_call_llm_attribution`` resets the contextvar unconditionally (no
    bleed into the caller's context) and flushes even when the body crashes;
  * ``stream_sentence_tts`` counts a byte-cache hit as ₹0 visibility instead
    of paid characters.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.services.call_cost_recorder as rec
from app.services.call_usage import (
    CallUsage,
    begin_call_usage,
    current_call_usage,
    end_call_usage,
    llm_cost_inr,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── complete_nano meters once ─────────────────────────────────────────────────


def _patch_nano(monkeypatch, payload):
    from app.services import llm_pool as pool_mod
    from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM

    member = SimpleNamespace(api_key="k", key_id="m1", endpoint="https://x", deployment="d")
    monkeypatch.setattr(pool_mod.LLMPool, "members", classmethod(lambda cls, pool="mini": [member]))

    async def _reserve(cls, est, pool="mini", sticky=True):
        return member

    async def _reconcile(cls, m, est, actual, pool="mini"):
        return None

    monkeypatch.setattr(pool_mod.LLMPool, "reserve", classmethod(_reserve))
    monkeypatch.setattr(pool_mod.LLMPool, "reconcile", classmethod(_reconcile))
    monkeypatch.setattr(
        AzureGroundedLLM, "_member_request",
        staticmethod(lambda m, msgs, stream=False, max_tokens=180: ("https://x/chat", {})),
    )

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class _Http:
        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(AzureGroundedLLM, "http", classmethod(lambda cls: _Http()))


@pytest.mark.asyncio
async def test_complete_nano_meters_once_into_sink(monkeypatch):
    from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM

    payload = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52,
            "prompt_tokens_details": {"cached_tokens": 8},
        },
    }
    _patch_nano(monkeypatch, payload)
    usage, token = begin_call_usage()
    try:
        out = await AzureGroundedLLM.complete_nano([{"role": "user", "content": "hi"}])
    finally:
        end_call_usage(token)
    assert out == "ok"
    assert usage.llm_input_tokens == 40
    assert usage.llm_output_tokens == 12
    assert usage.llm_cached_tokens == 8
    assert usage.llm_requests == 1  # exactly once — no double count


@pytest.mark.asyncio
async def test_complete_nano_no_sink_no_side_effects(monkeypatch):
    from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM

    payload = {"choices": [{"message": {"content": "ok"}}],
               "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}}
    _patch_nano(monkeypatch, payload)
    assert current_call_usage() is None  # no session
    out = await AzureGroundedLLM.complete_nano([{"role": "user", "content": "hi"}])
    assert out == "ok"
    assert current_call_usage() is None  # still no ambient sink


def test_meter_call_llm_responses_api_shape():
    """A /responses pool member reports input_tokens/output_tokens — both
    shapes must land identically."""
    from app.services.nokvo_one_voice_pipeline import _meter_call_llm

    usage, token = begin_call_usage()
    try:
        _meter_call_llm({
            "input_tokens": 10, "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 3},
        })
    finally:
        end_call_usage(token)
    assert usage.llm_input_tokens == 10
    assert usage.llm_output_tokens == 5
    assert usage.llm_cached_tokens == 3
    assert usage.llm_requests == 1


# ── attribute_post_call_llm: one guarded atomic UPDATE ────────────────────────


class _AttrSession:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.stmts = []
        self.committed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.stmts.append(stmt)
        return SimpleNamespace(rowcount=self.rowcount)

    async def commit(self):
        self.committed += 1


def _patch_session(monkeypatch, session):
    import app.db.session as dbs

    monkeypatch.setattr(dbs, "AsyncSessionLocal", lambda: session)


@pytest.mark.asyncio
async def test_attribute_post_call_llm_statement_shape(monkeypatch):
    session = _AttrSession(rowcount=1)
    _patch_session(monkeypatch, session)
    usage = CallUsage()
    usage.add_llm(prompt_tokens=1000, completion_tokens=500, cached_tokens=200)
    assert await rec.attribute_post_call_llm("call-1", usage) is True
    assert len(session.stmts) == 1 and session.committed == 1

    stmt = session.stmts[0]
    sql = str(stmt)
    assert "UPDATE call_costs" in sql
    assert "call_costs.call_id" in sql
    assert "cost_total_inr IS NOT NULL" in sql  # legacy total-only rows stay total-only
    assert "coalesce" in sql.lower()            # increments, never overwrites
    # The priced delta (the SAME tariff compute_cogs_inr uses) lands on BOTH
    # cost_llm_inr and cost_total_inr.
    delta = llm_cost_inr(1000, 500, 200)
    bound = list(stmt.compile().params.values())
    assert bound.count(delta) == 2


@pytest.mark.asyncio
async def test_attribute_post_call_llm_retries_then_drops(monkeypatch):
    session = _AttrSession(rowcount=0)  # no instrumented row matches
    _patch_session(monkeypatch, session)
    sleeps = []

    async def _sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(rec.asyncio, "sleep", _sleep)
    usage = CallUsage()
    usage.add_llm(prompt_tokens=10, completion_tokens=5)
    assert await rec.attribute_post_call_llm("call-x", usage, attempts=3) is False
    assert len(session.stmts) == 3      # retried
    assert len(sleeps) == 2             # no sleep after the last attempt


@pytest.mark.asyncio
async def test_attribute_post_call_llm_zero_usage_noop(monkeypatch):
    session = _AttrSession()
    _patch_session(monkeypatch, session)
    assert await rec.attribute_post_call_llm("call-1", CallUsage()) is False
    assert session.stmts == []          # nothing to attribute → no query


# ── post_call_llm_attribution: no bleed + crash-flush ─────────────────────────


@pytest.mark.asyncio
async def test_post_call_attribution_flushes_and_restores_outer_sink(monkeypatch):
    flushed = {}

    async def _fake_attr(call_id, usage, **kw):
        flushed["call_id"] = call_id
        flushed["tokens"] = (usage.llm_input_tokens, usage.llm_output_tokens)
        return True

    monkeypatch.setattr(rec, "attribute_post_call_llm", _fake_attr)

    outer, outer_token = begin_call_usage()  # a live outer sink (worst case)
    try:
        async with rec.post_call_llm_attribution("call-9") as usage:
            # The CM's fresh sink IS the ambient one inside the block…
            assert current_call_usage() is usage and usage is not outer
            usage.add_llm(prompt_tokens=7, completion_tokens=3)
        # …and the OUTER sink is restored (no bleed), unpolluted.
        assert current_call_usage() is outer
        assert outer.llm_input_tokens == 0
    finally:
        end_call_usage(outer_token)
    assert flushed == {"call_id": "call-9", "tokens": (7, 3)}


@pytest.mark.asyncio
async def test_post_call_attribution_crash_still_flushes(monkeypatch):
    """Azure bills for failed attempts too: a body that dies AFTER consuming
    tokens still attributes them, and the exception propagates."""
    flushed = {}

    async def _fake_attr(call_id, usage, **kw):
        flushed["tokens"] = usage.llm_input_tokens
        return True

    monkeypatch.setattr(rec, "attribute_post_call_llm", _fake_attr)
    with pytest.raises(ValueError):
        async with rec.post_call_llm_attribution("call-9") as usage:
            usage.add_llm(prompt_tokens=42, completion_tokens=1)
            raise ValueError("classifier blew up")
    assert flushed["tokens"] == 42
    assert current_call_usage() is None  # token reset even on crash


@pytest.mark.asyncio
async def test_post_call_attribution_zero_usage_no_flush(monkeypatch):
    called = {"n": 0}

    async def _fake_attr(call_id, usage, **kw):
        called["n"] += 1
        return True

    monkeypatch.setattr(rec, "attribute_post_call_llm", _fake_attr)
    async with rec.post_call_llm_attribution("call-9"):
        pass
    assert called["n"] == 0


# ── stream_sentence_tts: cache hit = ₹0 visibility, not paid chars ───────────


class _Ws:
    async def send_json(self, *_a, **_k):
        pass

    async def send_bytes(self, *_a, **_k):
        pass


@pytest.mark.asyncio
async def test_stream_sentence_tts_cache_hit_counts_free(monkeypatch):
    from app.core.config import settings
    from app.services.sarvam_voice_service import SarvamVoiceService

    monkeypatch.setattr(settings, "SARVAM_TTS_WS_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "SARVAM_TTS_STREAMING_ENABLED", False, raising=False)

    async def _synth_cached(tenant_res, text, **kw):
        return {"cached": True, "audios": []}

    monkeypatch.setattr(SarvamVoiceService, "synthesize", staticmethod(_synth_cached))
    tr = SimpleNamespace(provider_status={}, tenant_id="t1")
    usage, token = begin_call_usage()
    try:
        await SarvamVoiceService.stream_sentence_tts(_Ws(), tr, "hello there")
    finally:
        end_call_usage(token)
    assert usage.tts_cache_hits == 1
    assert usage.tts_cache_chars > 0
    assert usage.tts_characters == 0  # a hit never bills paid characters


@pytest.mark.asyncio
async def test_stream_sentence_tts_miss_bills_chars(monkeypatch):
    from app.core.config import settings
    from app.services.sarvam_voice_service import SarvamVoiceService

    monkeypatch.setattr(settings, "SARVAM_TTS_WS_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "SARVAM_TTS_STREAMING_ENABLED", False, raising=False)

    async def _synth_fresh(tenant_res, text, **kw):
        return {"cached": False, "audios": []}

    monkeypatch.setattr(SarvamVoiceService, "synthesize", staticmethod(_synth_fresh))
    tr = SimpleNamespace(provider_status={}, tenant_id="t1")
    usage, token = begin_call_usage()
    try:
        await SarvamVoiceService.stream_sentence_tts(_Ws(), tr, "hello there")
    finally:
        end_call_usage(token)
    assert usage.tts_characters > 0
    assert usage.tts_cache_hits == 0 and usage.tts_cache_chars == 0
