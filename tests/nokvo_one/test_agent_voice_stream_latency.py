from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.core.config import settings
from app.services.agent_outbound_context import OutboundCampaignContext
from app.services.agent_voice_stream_service import AgentVoiceStreamService, WarmSonioxTTSStream
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService
from app.services.sarvam_voice_service import SarvamVoiceService


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


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


def test_outbound_text_turn_does_not_emit_latency_guard(monkeypatch):
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
            call_id="call-outbound-no-guard",
            company_name="Raghava Constructions",
            outbound_context=outbound_context,
        )
    )

    sentences = [event for event in websocket.sent if event.get("type") == "agent_sentence"]
    assert sentences[0].get("source") != "latency_guard"
    assert sentences[0]["sentence"] == "Great, is this for self-use or investment?"
