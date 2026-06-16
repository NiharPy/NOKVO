from __future__ import annotations

import asyncio
import json
import logging
import re
from time import perf_counter
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.agent_outbound_context import OutboundCampaignContext
from app.services.agent_voice_stream_service import AgentVoiceStreamService, WarmSonioxTTSStream
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.nokvo_one_voice_stream_service import (
    NokvoOneVoiceStreamService,
    _latency_guard_text,
)
from app.services.sarvam_voice_service import SarvamVoiceService

# Every language NOKVO One supports — the sub-1s budget must hold on all of them.
_ALL_LANGUAGES = ["en", "hi", "ta", "te", "bn", "kn", "ml", "mr", "gu", "pa", "ur", "od"]
_LATENCY_LOGGER = "app.services.nokvo_one_voice_stream_service"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _DisconnectingWebSocket:
    def __init__(self, warm_started: asyncio.Event) -> None:
        self.accepted = False
        self.sent: list[dict] = []
        self.receive_called = asyncio.Event()
        self._warm_started = warm_started

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive(self) -> dict:
        self.receive_called.set()
        await asyncio.wait_for(self._warm_started.wait(), timeout=0.05)
        return {"type": "websocket.disconnect"}


def test_run_session_does_not_block_receive_loop_on_tts_prewarm(monkeypatch):
    async def fast_close(self) -> None:
        return None

    monkeypatch.setattr(settings, "AGENT_ANSWER_CACHE_ENABLED", False)
    monkeypatch.setattr(WarmSonioxTTSStream, "close", fast_close)

    async def scenario() -> _DisconnectingWebSocket:
        warm_started = asyncio.Event()

        async def slow_warm(self, language: str = "en") -> None:
            warm_started.set()
            await asyncio.sleep(1)

        monkeypatch.setattr(WarmSonioxTTSStream, "warm", slow_warm)

        websocket = _DisconnectingWebSocket(warm_started)
        tenant_res = SimpleNamespace(tenant_id="tenant-test", provider_status={})

        await asyncio.wait_for(
            AgentVoiceStreamService.run_session(websocket, tenant_res),
            timeout=0.25,
        )
        return websocket

    websocket = _run(scenario())

    assert websocket.accepted is True
    assert websocket.receive_called.is_set()


class _StreamingTurnWebSocket:
    def __init__(self, turn_fired: asyncio.Event) -> None:
        self.accepted = False
        self.sent: list[dict] = []
        self._turn_fired = turn_fired
        self._received_audio = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive(self) -> dict:
        if not self._received_audio:
            self._received_audio = True
            return {"bytes": b"pcm"}
        await asyncio.wait_for(self._turn_fired.wait(), timeout=0.7)
        return {"type": "websocket.disconnect"}


class _OneFinalTranscriptSTT:
    closed = False

    async def __aiter__(self):
        yield json.dumps({"text": "I need help", "is_final": True})
        while not self.closed:
            await asyncio.sleep(1)

    async def close(self) -> None:
        self.closed = True


def test_nokvo_streaming_eou_uses_sub_second_latency_budget(monkeypatch):
    async def scenario() -> _StreamingTurnWebSocket:
        turn_fired = asyncio.Event()

        async def noop(*args, **kwargs):
            return None

        async def fake_run_text_turn(*args, **kwargs):
            turn_fired.set()

        async def fake_connect_stt(*args, **kwargs):
            return _OneFinalTranscriptSTT()

        async def fake_company_name(*args, **kwargs):
            return "Test Clinic"

        monkeypatch.setattr(settings, "VOICE_EOU_DEBOUNCE_MS", 500)
        monkeypatch.setattr(settings, "VOICE_EOU_CONTINUATION_BONUS_MS", 1000)
        monkeypatch.setattr(settings, "AGENT_TRANSLATE_FOR_RETRIEVAL_ENABLED", False)
        monkeypatch.setattr(NokvoOneVoiceStreamService, "_company_name", staticmethod(fake_company_name))
        monkeypatch.setattr(NokvoOneVoiceStreamService, "_emit_runtime_status", staticmethod(noop))
        monkeypatch.setattr(NokvoOneVoiceStreamService, "_play_opener", staticmethod(noop))
        monkeypatch.setattr(NokvoOneVoiceStreamService, "_log_voice_call", staticmethod(noop))
        monkeypatch.setattr(NokvoOneVoiceStreamService, "_run_text_turn", staticmethod(fake_run_text_turn))
        monkeypatch.setattr("app.services.nokvo_one_voice_stream_service.AgentSessionStore.set_state", noop)
        monkeypatch.setattr(SarvamVoiceService, "connect_stt", staticmethod(fake_connect_stt))
        monkeypatch.setattr(SarvamVoiceService, "send_stt_audio", staticmethod(noop))
        monkeypatch.setattr(SarvamVoiceService, "parse_stt_message", staticmethod(lambda raw: json.loads(raw)))

        websocket = _StreamingTurnWebSocket(turn_fired)
        tenant_res = SimpleNamespace(
            tenant_id="tenant-test",
            organization_id="org-test",
            provider_status={},
        )
        await asyncio.wait_for(
            NokvoOneVoiceStreamService.run_session(
                websocket,
                tenant_res,
                campaign_context={"opening_message": "Hello"},
            ),
            timeout=0.9,
        )
        return websocket

    websocket = _run(scenario())

    assert websocket.accepted is True


class _BurstSTT:
    """Yields two finals; the second only AFTER turn 1 has been dispatched, so
    turn 1 is still in-flight (pre-speech) when turn 2 fires — the burst that
    silently dropped turns in the LangSmith call."""

    def __init__(self, turn1_dispatched: asyncio.Event) -> None:
        self.closed = False
        self._turn1 = turn1_dispatched

    async def __aiter__(self):
        yield json.dumps({"text": "at around 2 PM", "is_final": True})
        try:
            await asyncio.wait_for(self._turn1.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        yield json.dumps({"text": "Alright, thank you.", "is_final": True})
        while not self.closed:
            await asyncio.sleep(0.5)

    async def close(self) -> None:
        self.closed = True


class _BurstWebSocket:
    def __init__(self, done: asyncio.Event) -> None:
        self.accepted = False
        self.sent: list[dict] = []
        self._done = done
        self._fed = 0

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive(self) -> dict:
        if self._fed < 2:
            self._fed += 1
            return {"bytes": b"pcm"}
        await asyncio.wait_for(self._done.wait(), timeout=3.0)
        return {"type": "websocket.disconnect"}


def test_pre_speech_turn_is_folded_not_dropped(monkeypatch):
    """Carry-forward (Fix #2): when a fresh utterance fires while the previous
    turn is still pre-speech, the previous turn's text must be FOLDED into the
    new turn, not silently dropped on drain."""
    async def scenario() -> list[str]:
        turn1_dispatched = asyncio.Event()
        done = asyncio.Event()
        calls: list[str] = []

        async def noop(*args, **kwargs):
            return None

        async def fake_run_text_turn(websocket, tenant_res, text, **kwargs):
            calls.append(text)
            if len(calls) == 1:
                turn1_dispatched.set()
                # Stay in-flight & pre-speech (never marks turn_state speaking)
                # until the carry-forward path drains/cancels us.
                await asyncio.sleep(5)
            else:
                done.set()

        async def fake_connect_stt(*args, **kwargs):
            return _BurstSTT(turn1_dispatched)

        async def fake_company_name(*args, **kwargs):
            return "Raghava Constructions"

        monkeypatch.setattr(settings, "VOICE_EOU_COMPLETE_MS", 200)
        monkeypatch.setattr(settings, "VOICE_EOU_NEUTRAL_MS", 400)
        monkeypatch.setattr(settings, "VOICE_EOU_DEBOUNCE_MS", 500)
        monkeypatch.setattr(settings, "VOICE_EOU_CONTINUATION_BONUS_MS", 0)
        monkeypatch.setattr(settings, "AGENT_TRANSLATE_FOR_RETRIEVAL_ENABLED", False)
        monkeypatch.setattr(NokvoOneVoiceStreamService, "_company_name", staticmethod(fake_company_name))
        monkeypatch.setattr(NokvoOneVoiceStreamService, "_emit_runtime_status", staticmethod(noop))
        monkeypatch.setattr(NokvoOneVoiceStreamService, "_play_opener", staticmethod(noop))
        monkeypatch.setattr(NokvoOneVoiceStreamService, "_log_voice_call", staticmethod(noop))
        monkeypatch.setattr(NokvoOneVoiceStreamService, "_run_text_turn", staticmethod(fake_run_text_turn))
        monkeypatch.setattr("app.services.nokvo_one_voice_stream_service.AgentSessionStore.set_state", noop)
        monkeypatch.setattr(SarvamVoiceService, "connect_stt", staticmethod(fake_connect_stt))
        monkeypatch.setattr(SarvamVoiceService, "send_stt_audio", staticmethod(noop))
        monkeypatch.setattr(SarvamVoiceService, "parse_stt_message", staticmethod(lambda raw: json.loads(raw)))

        websocket = _BurstWebSocket(done)
        tenant_res = SimpleNamespace(tenant_id="tenant-test", organization_id="org-test", provider_status={})
        await asyncio.wait_for(
            NokvoOneVoiceStreamService.run_session(
                websocket,
                tenant_res,
                campaign_context={"opening_message": "Hello"},
            ),
            timeout=4.0,
        )
        return calls

    calls = _run(scenario())
    assert calls[0] == "at around 2 PM"
    # Turn 2 carries BOTH utterances — the first wasn't dropped.
    assert "at around 2 PM" in calls[1]
    assert "Alright, thank you." in calls[1]


class _CollectingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def test_text_turn_emits_latency_guard_before_slow_llm_sentence(monkeypatch):
    async def fake_stream_answer_sentences(*args, **kwargs):
        await asyncio.sleep(0.12)
        yield {"type": "sentence", "text": "The real answer is ready.", "tone": "neutral"}
        yield {
            "type": "final",
            "answer": "The real answer is ready.",
            "refused": False,
            "chunks": [],
            "citations": [],
            "runtime": {"mode": "test"},
        }

    async def fake_tts(*args, **kwargs):
        return {"audios": ["audio"], "first_audio_ms": 1}

    async def noop(*args, **kwargs):
        return None

    async def empty_state(*args, **kwargs):
        return {}

    monkeypatch.setattr(settings, "VOICE_FIRST_SENTENCE_TIMEOUT_MS", 50)
    monkeypatch.setattr(settings, "VOICE_LATENCY_GUARD_CONTENT_GRACE_MS", 0)  # exercise the filler path
    monkeypatch.setattr(NokvoOneVoicePipeline, "stream_answer_sentences", staticmethod(fake_stream_answer_sentences))
    monkeypatch.setattr(SarvamVoiceService, "stream_sentence_tts", staticmethod(fake_tts))
    monkeypatch.setattr("app.services.nokvo_one_voice_stream_service.AgentSessionStore.append_turn", noop)

    websocket = _CollectingWebSocket()
    tenant_res = SimpleNamespace(tenant_id="tenant-test", organization_id="org-test", provider_status={})

    _run(
        NokvoOneVoiceStreamService._run_text_turn(
            websocket,
            tenant_res,
            "Tell me about appointments",
            language="en",
            call_id="call-latency-guard",
            company_name="Test Clinic",
        )
    )

    sentences = [event for event in websocket.sent if event.get("type") == "agent_sentence"]
    assert sentences[0]["source"] == "latency_guard"
    assert sentences[0]["first_sentence_ms"] < 100
    assert sentences[1]["sentence"] == "The real answer is ready."
    final = next(event for event in websocket.sent if event.get("type") == "agent_answer")
    assert final["answer"] == "The real answer is ready."


def test_grace_peek_skips_filler_when_content_is_imminent(monkeypatch, caplog):
    """#2: if the real answer lands within the grace window just past the budget,
    the filler is SKIPPED — content shouldn't queue behind ~1s of filler audio in
    the single TTS pump. Also asserts the #1 content-latency record is emitted."""
    async def fake_stream_answer_sentences(*args, **kwargs):
        await asyncio.sleep(0.08)  # past the 40ms budget, well within the 400ms grace
        yield {"type": "sentence", "text": "Skyline Heights is in Tukkuguda.", "tone": "neutral"}
        yield {
            "type": "final",
            "answer": "Skyline Heights is in Tukkuguda.",
            "refused": False,
            "chunks": [],
            "citations": [],
            "runtime": {"mode": "test"},
        }

    async def fake_tts(*args, **kwargs):
        return {"audios": ["audio"], "first_audio_ms": 1}

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(settings, "VOICE_FIRST_SENTENCE_TIMEOUT_MS", 40)
    monkeypatch.setattr(settings, "VOICE_LATENCY_GUARD_FLOOR_MS", 20)
    monkeypatch.setattr(settings, "VOICE_LATENCY_GUARD_CONTENT_GRACE_MS", 400)
    monkeypatch.setattr(NokvoOneVoicePipeline, "stream_answer_sentences", staticmethod(fake_stream_answer_sentences))
    monkeypatch.setattr(SarvamVoiceService, "stream_sentence_tts", staticmethod(fake_tts))
    monkeypatch.setattr("app.services.nokvo_one_voice_stream_service.AgentSessionStore.append_turn", noop)

    websocket = _CollectingWebSocket()
    tenant_res = SimpleNamespace(tenant_id="tenant-test", organization_id="org-test", provider_status={})

    with caplog.at_level(logging.INFO, logger=_LATENCY_LOGGER):
        _run(
            NokvoOneVoiceStreamService._run_text_turn(
                websocket,
                tenant_res,
                "Where is Skyline Heights",
                language="en",
                call_id="call-grace-peek",
                company_name="Raghava Constructions",
                eou_fired_at=perf_counter(),
                eou_tier="neutral",
            )
        )

    sentences = [event for event in websocket.sent if event.get("type") == "agent_sentence"]
    # No filler — content was imminent and spoken directly.
    assert all(s.get("source") != "latency_guard" for s in sentences)
    assert sentences[0]["sentence"] == "Skyline Heights is in Tukkuguda."
    # #1: the content-latency record fires, with no preceding filler.
    content = next(
        rec.getMessage() for rec in caplog.records if "NOKVO-LATENCY-CONTENT" in rec.getMessage()
    )
    assert "filler_preceded=False" in content


def test_outbound_text_turn_emits_localized_bridge_filler(monkeypatch):
    # WS3: outbound now gets the latency backstop too — but as a short
    # conversational BRIDGE ("Just a moment…"/native), not the inbound "one
    # moment, I'm checking that" hold that reads as a stalled queue on a sales
    # call. The bridge still keeps SOME audio within the sub-1s budget.
    async def fake_stream_answer_sentences(*args, **kwargs):
        await asyncio.sleep(0.08)
        yield {"type": "sentence", "text": "Great, is this for self-use or investment?", "tone": "question"}
        yield {
            "type": "final",
            "answer": "Great, is this for self-use or investment?",
            "refused": False,
            "chunks": [],
            "citations": [],
            "runtime": {"mode": "test"},
        }

    async def fake_tts(*args, **kwargs):
        return {"audios": ["audio"], "first_audio_ms": 1}

    async def noop(*args, **kwargs):
        return None

    async def empty_state(*args, **kwargs):
        return {}

    outbound_context = OutboundCampaignContext(
        campaign_id="campaign-latency",
        name="Raghava Skyline",
        goal="Confirm if the prospect can come for site visit",
        agent_prompt="",
        objectives=["Confirm time to talk", "Book site visit"],
        exit_conditions=["Not interested"],
        tone="warm",
        doc_text=None,
        caller_name="Riya",
        company_name="Raghava Constructions",
        pitch_summary="Raghava Skyline in Kokapet",
    )

    monkeypatch.setattr(settings, "VOICE_FIRST_SENTENCE_TIMEOUT_MS", 20)
    monkeypatch.setattr(settings, "VOICE_LATENCY_GUARD_CONTENT_GRACE_MS", 0)  # exercise the bridge-filler path
    monkeypatch.setattr(NokvoOneVoicePipeline, "stream_answer_sentences", staticmethod(fake_stream_answer_sentences))
    monkeypatch.setattr(SarvamVoiceService, "stream_sentence_tts", staticmethod(fake_tts))
    monkeypatch.setattr("app.services.nokvo_one_voice_stream_service.AgentSessionStore.append_turn", noop)
    monkeypatch.setattr("app.services.nokvo_one_voice_stream_service.AgentSessionStore.get_state", empty_state)
    monkeypatch.setattr("app.services.nokvo_one_voice_stream_service.AgentSessionStore.merge_state", empty_state)

    websocket = _CollectingWebSocket()
    tenant_res = SimpleNamespace(tenant_id="tenant-test", organization_id="org-test", provider_status={})

    _run(
        NokvoOneVoiceStreamService._run_text_turn(
            websocket,
            tenant_res,
            "Yeah",
            language="en",
            call_id="call-outbound-bridge",
            company_name="Raghava Constructions",
            outbound_context=outbound_context,
        )
    )

    sentences = [event for event in websocket.sent if event.get("type") == "agent_sentence"]
    # The bridge fires first (LLM is slower than the 20ms ceiling)…
    assert sentences[0]["source"] == "latency_guard"
    # …and it's the OUTBOUND bridge register, not the inbound hold.
    assert sentences[0]["sentence"] == _latency_guard_text("en", "outbound")
    assert sentences[0]["sentence"] != _latency_guard_text("en", "inbound")
    # The real answer still follows.
    assert sentences[1]["sentence"] == "Great, is this for self-use or investment?"


def _parse_latency_record(caplog) -> dict:
    """Pull the single NOKVO-LATENCY-TURN record (WS5) out of captured logs."""
    for rec in caplog.records:
        msg = rec.getMessage()
        if "NOKVO-LATENCY-TURN" in msg:
            return {
                "raw": msg,
                "eos_to_first_audio_ms": int(re.search(r"eos_to_first_audio_ms=(\d+)", msg).group(1)),
                "source": re.search(r"source=(\w+)", msg).group(1),
                "direction": re.search(r"direction=(\w+)", msg).group(1),
                "language": re.search(r"language=(\w+)", msg).group(1),
                "within_budget": re.search(r"within_budget=(\w+)", msg).group(1) == "True",
            }
    raise AssertionError("no NOKVO-LATENCY-TURN record emitted")


def _make_outbound_context() -> OutboundCampaignContext:
    return OutboundCampaignContext(
        campaign_id="campaign-matrix",
        name="Matrix Towers",
        goal="Book a site visit",
        agent_prompt="",
        objectives=["Book site visit"],
        exit_conditions=["Not interested"],
        tone="warm",
        doc_text=None,
        caller_name="Riya",
        company_name="Matrix Constructions",
        pitch_summary="Matrix Towers in Kokapet",
    )


@pytest.mark.parametrize("language", _ALL_LANGUAGES)
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
@pytest.mark.parametrize("llm_path", ["fast", "slow"])
def test_eos_to_first_audio_under_budget_all_languages_both_directions(
    monkeypatch, caplog, language, direction, llm_path
):
    """The whole matrix: 12 languages × {inbound, outbound} × {fast LLM, slow
    LLM}. With the end-of-speech anchor threaded in, eos→first_audio must stay
    under VOICE_LATENCY_BUDGET_MS — served by the REAL answer on the fast path
    and by the localized FILLER/bridge on the slow path."""
    is_slow = llm_path == "slow"

    async def fake_stream_answer_sentences(*args, **kwargs):
        # Slow LLM: take longer than the guard ceiling so the filler wins.
        # Fast LLM: yield almost immediately so the real answer wins.
        await asyncio.sleep(0.09 if is_slow else 0.005)
        yield {"type": "sentence", "text": "Here is the real answer.", "tone": "neutral"}
        yield {
            "type": "final",
            "answer": "Here is the real answer.",
            "refused": False,
            "chunks": [],
            "citations": [],
            "runtime": {"mode": "test"},
        }

    async def fake_tts(*args, **kwargs):
        return {"audios": ["audio"], "first_audio_ms": 1}

    async def noop(*args, **kwargs):
        return None

    async def empty_state(*args, **kwargs):
        return {}

    # Small ceiling keeps the slow-path filler fast & deterministic; the dynamic
    # budget math (floor/margin) is unit-checked separately. The eos anchor is
    # "just now", so eos→first_audio ≈ the guard wait, comfortably sub-budget.
    monkeypatch.setattr(settings, "VOICE_FIRST_SENTENCE_TIMEOUT_MS", 60)
    monkeypatch.setattr(settings, "VOICE_LATENCY_GUARD_FLOOR_MS", 20)
    # Grace 0 here so the slow path deterministically exercises the FILLER; the
    # grace-peek skip path has its own test below.
    monkeypatch.setattr(settings, "VOICE_LATENCY_GUARD_CONTENT_GRACE_MS", 0)
    monkeypatch.setattr(NokvoOneVoicePipeline, "stream_answer_sentences", staticmethod(fake_stream_answer_sentences))
    monkeypatch.setattr(SarvamVoiceService, "stream_sentence_tts", staticmethod(fake_tts))
    monkeypatch.setattr("app.services.nokvo_one_voice_stream_service.AgentSessionStore.append_turn", noop)
    monkeypatch.setattr("app.services.nokvo_one_voice_stream_service.AgentSessionStore.get_state", empty_state)
    monkeypatch.setattr("app.services.nokvo_one_voice_stream_service.AgentSessionStore.merge_state", empty_state)

    outbound_context = _make_outbound_context() if direction == "outbound" else None
    websocket = _CollectingWebSocket()
    tenant_res = SimpleNamespace(tenant_id="tenant-test", organization_id="org-test", provider_status={})

    with caplog.at_level(logging.INFO, logger=_LATENCY_LOGGER):
        _run(
            NokvoOneVoiceStreamService._run_text_turn(
                websocket,
                tenant_res,
                "Yes please tell me",
                language=language,
                call_id=f"call-{language}-{direction}-{llm_path}",
                company_name="Matrix Constructions",
                outbound_context=outbound_context,
                eou_fired_at=perf_counter(),
                eou_tier="neutral",
            )
        )

    record = _parse_latency_record(caplog)
    assert record["language"] == SarvamVoiceService.normalize_language(language)
    assert record["direction"] == direction
    # The hard guarantee: end-of-speech → first audio is strictly sub-1s.
    assert record["eos_to_first_audio_ms"] < settings.VOICE_LATENCY_BUDGET_MS
    assert record["within_budget"] is True
    # Right source for the path: real answer when the LLM is quick, localized
    # filler/bridge when it's slow.
    assert record["source"] == ("filler" if is_slow else "real")

    sentences = [event for event in websocket.sent if event.get("type") == "agent_sentence"]
    if is_slow:
        assert sentences[0]["source"] == "latency_guard"
        assert sentences[0]["sentence"] == _latency_guard_text(language, direction)
