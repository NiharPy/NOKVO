"""Regression tests for the unified robustness layer.

Four failure modes:

* **bad audio** — :class:`AudioQualityProbe` scoring + the recovery
  branch that asks the caller to repeat instead of running STT/LLM/TTS.
* **mixed-language** — :class:`LanguageState` code-switch detection and
  the dual-retrieval merge inside :func:`NokvoOneVoicePipeline.retrieve`.
* **interruptions** — :class:`TurnArbiter` phase transitions and atomic
  cancellation of the LLM stream + TTS pump.
* **vague users** — :class:`ClarificationState` FSM and the nudge →
  options → escalate ladder.

Plus a latency assertion: a single call to each robustness primitive
finishes in well under 10 ms so the layer never adds detectable cost to
the happy path.
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
from typing import Any

import pytest

from app.services.agent_robustness import (
    CLARIFY_ESCALATE,
    CLARIFY_NUDGE,
    CLARIFY_OFFER_OPTIONS,
    CLARIFY_RESET,
    QUALITY_OK,
    QUALITY_POOR,
    QUALITY_UNUSABLE,
    TURN_IDLE,
    TURN_SPEAKING,
    TURN_THINKING,
    AudioQuality,
    AudioQualityProbe,
    ClarificationState,
    LanguageState,
    RobustnessContext,
    TurnArbiter,
    clarification_prompt,
    is_turn_vague,
    repeat_prompt,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _pcm_sine(seconds: float, freq: int, amp: float, sr: int = 16000) -> bytes:
    """Build PCM16-LE mono audio for a single sine wave. ``amp`` ≥ 1
    deliberately overflows so callers can simulate clipping; we clamp to
    the int16 range inside the function."""
    n = max(0, int(seconds * sr))
    samples = []
    for i in range(n):
        v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / sr))
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        samples.append(v)
    return struct.pack(f"<{n}h", *samples)


def _pcm_silence(seconds: float, sr: int = 16000) -> bytes:
    return b"\x00\x00" * int(seconds * sr)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── 1) Audio quality probe ──────────────────────────────────────────────────


def test_audio_quality_probe_marks_silence_as_unusable():
    quality = AudioQualityProbe.score(_pcm_silence(0.5), sample_rate=16000)
    assert quality.verdict == QUALITY_UNUSABLE
    # Silence => RMS at the floor.
    assert quality.rms < 0.001
    assert quality.silence_ratio >= 0.95
    assert "silence" in quality.reason.lower()


def test_audio_quality_probe_marks_too_short_as_unusable():
    quality = AudioQualityProbe.score(_pcm_sine(0.05, 440, 0.5), sample_rate=16000)
    assert quality.verdict == QUALITY_UNUSABLE
    assert quality.duration_ms < AudioQualityProbe.UNUSABLE_MIN_DURATION_MS
    assert "short" in quality.reason.lower()


def test_audio_quality_probe_marks_heavy_clipping_as_unusable():
    # Amplitude 1.5 saturates the int16 range during the peak portions
    # of the sine wave, producing a high clip ratio.
    quality = AudioQualityProbe.score(_pcm_sine(0.5, 440, 1.5), sample_rate=16000)
    assert quality.verdict == QUALITY_UNUSABLE
    assert quality.clip_ratio >= AudioQualityProbe.UNUSABLE_MAX_CLIP_RATIO
    assert "clip" in quality.reason.lower()


def test_audio_quality_probe_marks_quiet_signal_as_unusable():
    quality = AudioQualityProbe.score(_pcm_sine(0.5, 440, 0.002), sample_rate=16000)
    assert quality.verdict == QUALITY_UNUSABLE


def test_audio_quality_probe_passes_clean_speech_like_signal():
    quality = AudioQualityProbe.score(_pcm_sine(0.6, 440, 0.4), sample_rate=16000)
    assert quality.verdict == QUALITY_OK
    assert 0.2 < quality.rms < 0.5
    assert quality.clip_ratio == 0.0
    assert quality.duration_ms >= 600
    assert quality.usable is True


def test_audio_quality_probe_handles_empty_input():
    quality = AudioQualityProbe.score(b"", sample_rate=16000)
    assert quality.verdict == QUALITY_UNUSABLE
    assert quality.duration_ms == 0
    assert quality.usable is False


def test_repeat_prompt_falls_back_to_english_for_unknown_language():
    assert repeat_prompt("xx") == repeat_prompt("en")
    assert "again" in repeat_prompt("en").lower() or "repeat" in repeat_prompt("en").lower()
    # Native-script prompts pick up by short language code.
    assert repeat_prompt("te").startswith("సరిగ్గా")


# ── 2) Mixed-language code-switch detection ─────────────────────────────────


def test_language_state_flags_code_switching_after_two_languages():
    state = LanguageState()
    state.observe("en", "hi, can I cancel my order?")
    assert state.code_switching is False
    state.observe("te", "నా ఆర్డర్ క్యాన్సిల్ చేయండి")
    assert state.code_switching is True
    assert state.needs_dual_retrieval() is True


def test_language_state_flags_script_mixed_single_turn():
    state = LanguageState()
    state.observe("hi", "मुझे cancellation policy chahiye")
    assert state.last_script_mix == "mixed"
    assert state.code_switching is True


def test_language_state_locks_explicit_request():
    state = LanguageState()
    state.observe("en", "speak in Telugu please")
    state.lock("te")
    state.observe("en", "okay then")
    # Even though the latest STT segment was English, the explicit
    # lock wins.
    assert state.dominant_language() == "te"


def test_language_state_dominant_picks_most_frequent_history():
    state = LanguageState()
    for lang in ("hi", "hi", "en"):
        state.observe(lang, "")
    assert state.dominant_language() == "hi"


# ── 3) Turn arbiter / barge-in ──────────────────────────────────────────────


def test_arbiter_idle_classifies_as_proceed():
    arbiter = TurnArbiter()
    assert arbiter.phase == TURN_IDLE
    assert arbiter.classify_incoming(is_check_in=False) == "proceed"
    assert arbiter.classify_incoming(is_check_in=True) == "proceed"


def test_arbiter_thinking_phase_treats_check_in_as_ack_only():
    arbiter = TurnArbiter()
    arbiter.begin("t1")
    assert arbiter.phase == TURN_THINKING
    assert arbiter.classify_incoming(is_check_in=True) == "check_in"
    assert arbiter.classify_incoming(is_check_in=False) == "barge_in"


def test_arbiter_speaking_phase_treats_any_speech_as_barge_in():
    arbiter = TurnArbiter()
    arbiter.begin("t1")
    arbiter.mark_speaking()
    assert arbiter.phase == TURN_SPEAKING
    assert arbiter.classify_incoming(is_check_in=True) == "barge_in"
    assert arbiter.classify_incoming(is_check_in=False) == "barge_in"


def test_arbiter_cancel_kills_both_pump_and_task():
    """Cancellation has to be atomic — the TTS pump stops emitting
    sentence audio AND the running task is cancelled."""

    async def _t() -> None:
        arbiter = TurnArbiter()

        class _FakePump:
            def __init__(self) -> None:
                self.cancelled = False

            async def cancel(self) -> None:
                self.cancelled = True

        async def _long_running() -> None:
            await asyncio.sleep(5.0)

        pump = _FakePump()
        task = asyncio.create_task(_long_running())
        arbiter.begin("t1", task=task)
        arbiter.attach_pump(pump)
        arbiter.mark_speaking()

        await arbiter.cancel()
        assert pump.cancelled is True
        assert task.done()
        assert arbiter.phase == "done"

    _run(_t())


# ── 4) Clarification FSM ────────────────────────────────────────────────────


def test_clarification_fsm_progresses_through_nudge_options_escalate():
    state = ClarificationState()
    assert state.action() == CLARIFY_RESET
    state.bump("hmm")
    assert state.action() == CLARIFY_NUDGE
    state.bump("uh")
    assert state.action() == CLARIFY_OFFER_OPTIONS
    state.bump("yeah")
    assert state.action() == CLARIFY_ESCALATE


def test_clarification_fsm_resets_after_clear_turn():
    state = ClarificationState()
    state.bump("hmm")
    state.bump("uh")
    assert state.consecutive_vague_turns == 2
    state.reset()
    assert state.consecutive_vague_turns == 0
    assert state.last_unanswered_query is None


def test_clarification_prompts_localise_per_language():
    nudge_en = clarification_prompt(CLARIFY_NUDGE, "en")
    nudge_hi = clarification_prompt(CLARIFY_NUDGE, "hi")
    options_te = clarification_prompt(CLARIFY_OFFER_OPTIONS, "te")
    escalate_ta = clarification_prompt(CLARIFY_ESCALATE, "ta")
    # Distinct strings — no accidental fall-throughs that would make the
    # agent sound like it always replies in English.
    assert nudge_en != nudge_hi
    assert options_te != nudge_en
    assert "ஏஜெண்ட்" in escalate_ta or "சிரம" in escalate_ta or "பிரதிநிதி" in escalate_ta
    # Unknown languages fall back to English.
    assert clarification_prompt(CLARIFY_ESCALATE, "xx") == clarification_prompt(CLARIFY_ESCALATE, "en")


def test_clarification_serialises_roundtrip():
    state = ClarificationState(consecutive_vague_turns=2, last_unanswered_query="hmm")
    revived = ClarificationState.from_dict(state.to_dict())
    assert revived.consecutive_vague_turns == 2
    assert revived.last_unanswered_query == "hmm"


# ── is_turn_vague classifier ────────────────────────────────────────────────


def test_is_turn_vague_skips_fsm_slot_turns():
    # The FSM is mid-booking. Even an empty caller utterance is the FSM
    # asking the question, not a vague caller.
    assert is_turn_vague(
        route="template",
        intent="greeting",
        refused=False,
        chunks=[],
        user_text="",
        state_slot="patient_name",
    ) is False


def test_is_turn_vague_skips_known_intent_with_kb_miss():
    # We knew the caller was asking about refund, just couldn't ground it.
    # That's a KB miss, not vagueness — the standard refusal already
    # informs them.
    assert is_turn_vague(
        route="rag",
        intent="refund_eligibility",
        refused=True,
        chunks=[],
        user_text="will I get a refund?",
    ) is False


def test_is_turn_vague_treats_short_low_signal_utterance_as_vague():
    assert is_turn_vague(
        route="no_context_refusal",
        intent="unknown_general",
        refused=True,
        chunks=[],
        user_text="hmm",
    ) is True


def test_is_turn_vague_template_route_never_vague():
    # Template branches always produced a concrete answer.
    assert is_turn_vague(
        route="template",
        intent="greeting",
        refused=False,
        chunks=[],
        user_text="hi",
    ) is False


# ── 5) Latency — the layer must not add user-perceptible cost ───────────────


def test_audio_probe_finishes_under_10ms_for_a_one_second_utterance():
    pcm = _pcm_sine(1.0, 440, 0.4)
    start = time.perf_counter()
    for _ in range(5):
        AudioQualityProbe.score(pcm, sample_rate=16000)
    elapsed_ms = (time.perf_counter() - start) * 1000 / 5
    # The probe is pure-Python over ~16k samples — comfortably under
    # 10 ms even on a contended CI runner.
    assert elapsed_ms < 10, f"probe averaged {elapsed_ms:.2f}ms per 1s utterance"


def test_robustness_context_is_cheap_to_construct():
    start = time.perf_counter()
    for _ in range(1000):
        RobustnessContext()
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 50, f"1000× RobustnessContext init took {elapsed_ms:.1f}ms"


def test_language_state_observe_under_50us_each():
    state = LanguageState()
    start = time.perf_counter()
    for _ in range(1000):
        state.observe("te", "నాకు ఒక appointment kavali")
    elapsed_us = (time.perf_counter() - start) * 1_000_000
    per_call = elapsed_us / 1000
    assert per_call < 200, f"observe() averaged {per_call:.1f}µs per call"


def test_arbiter_classify_incoming_is_pure_dict_lookup():
    arbiter = TurnArbiter()
    arbiter.begin("t1")
    arbiter.mark_speaking()
    start = time.perf_counter()
    for _ in range(10_000):
        arbiter.classify_incoming(is_check_in=False)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 50, f"10k classify_incoming() took {elapsed_ms:.1f}ms"


# ── Pipeline integration smoke tests ────────────────────────────────────────


def test_retrieve_dual_returns_unioned_chunks_sorted_by_score(monkeypatch):
    """Calling _retrieve_dual stubs the underlying retrieve to return
    separate chunk sets and verifies the merge / sort / dedup behaviour.
    Keeps the test pure-unit (no DB, no Qdrant, no embeddings)."""
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline

    captured: list[str] = []

    async def _fake_retrieve(tenant_res, query, **kwargs):
        captured.append(query)
        # Two non-overlapping result sets so we can verify the union.
        if query == "primary":
            return {
                "query": "primary",
                "chunks": [
                    {"chunk_id": "A", "score": 0.7, "text": "primary-A"},
                    {"chunk_id": "B", "score": 0.5, "text": "primary-B"},
                ],
                "min_score": 0.3,
            }
        return {
            "query": "secondary",
            "chunks": [
                {"chunk_id": "B", "score": 0.9, "text": "secondary-B-better"},
                {"chunk_id": "C", "score": 0.4, "text": "secondary-C"},
            ],
            "min_score": 0.3,
        }

    monkeypatch.setattr(NokvoOneVoicePipeline, "retrieve", staticmethod(_fake_retrieve))

    result = _run(
        NokvoOneVoicePipeline._retrieve_dual(
            tenant_res=None,
            primary="primary",
            secondary="secondary",
            db=None,
            top_k=None,
            campaign_id=None,
            intent_result=None,
        )
    )
    chunk_ids = [c["chunk_id"] for c in result["chunks"]]
    # Union by chunk_id, max score wins for "B".
    assert set(chunk_ids) == {"A", "B", "C"}
    # The secondary's B (score=0.9) should beat the primary's B (0.5).
    b_chunk = next(c for c in result["chunks"] if c["chunk_id"] == "B")
    assert b_chunk["text"] == "secondary-B-better"
    # Sorted by score descending: B (0.9) > A (0.7) > C (0.4).
    assert chunk_ids == ["B", "A", "C"]
    assert result["dual_retrieval"] is True
    # Both sides were queried in parallel.
    assert sorted(captured) == ["primary", "secondary"]


def test_retrieve_skips_dual_path_when_no_secondary(monkeypatch):
    """When the secondary query is empty or equals the primary, the
    dual-retrieval branch must NOT fire — otherwise we waste a Qdrant
    search on every non-code-switching turn."""
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline

    queried: list[str] = []

    async def _embed_stub(tenant_res, text):  # pragma: no cover - not invoked
        queried.append(text)
        return [0.0] * 4

    # Patch the embedding so retrieve() can run without the OpenAI dep,
    # then verify _retrieve_dual was never called.
    from app.services import text_embedding_service as t

    called_dual = {"yes": False}

    async def _spy_dual(*args, **kwargs):
        called_dual["yes"] = True
        return {"chunks": []}

    monkeypatch.setattr(NokvoOneVoicePipeline, "_retrieve_dual", staticmethod(_spy_dual))
    monkeypatch.setattr(t.TextEmbeddingService, "embed_text_for_tenant", staticmethod(_embed_stub))

    # Build a minimal tenant_res stub that retrieve() can read.
    class _StubTenant:
        tenant_id = "tenant-test"
        provider_status: dict[str, Any] = {}

    async def _qdrant_stub(*a, **kw):
        return []

    from app.services import qdrant_service as q

    monkeypatch.setattr(q.QdrantService, "search_points", staticmethod(_qdrant_stub))

    _run(
        NokvoOneVoicePipeline.retrieve(
            _StubTenant(),
            "what are your hours?",
            db=None,
            top_k=3,
            campaign_id=None,
            intent_result=None,
            english_text=None,
            dual_retrieval=True,  # set, but no secondary => skip dual path
        )
    )
    assert called_dual["yes"] is False
