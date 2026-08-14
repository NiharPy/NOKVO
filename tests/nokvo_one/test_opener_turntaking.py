"""The outbound opener inside the turn-taking state machine.

The opener is the first six-to-eight seconds of an outbound call — the window
where a stranger decides whether to stay on the line — and it was the ONE speech
path that ran outside the turn arbiter. It was awaited before the receive loop
started (so the agent was deaf while greeting), and it returned when the audio
was handed to the telephony socket rather than when the callee had heard it (so
the arbiter read IDLE throughout). A "Hello?" over the intro was therefore
classified as a fresh turn, never a barge-in; nothing flushed the queued audio;
and the agent's reply stacked up behind an opener still playing.

Covered here (the units):
  * the adapter's PLAYOUT CLOCK — how long the callee still has to listen;
  * ``_play_opener``'s arbiter lifecycle: SPEAKING before the first audio frame,
    DONE only once playback has actually finished;
  * ``clear_playback`` resetting the clock, which is what makes a barge-in stop
    the agent mid-sentence instead of only stopping further generation.

The receive-loop wiring (opener as an arbiter-registered task, speech_end
releasing the listen window, clear_playback on a confirmed barge-in) is nested in
``run_session``; it is verified by a real call, per the same convention as
test_outbound_humanization.py.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.agent_robustness import TURN_DONE, TURN_SPEAKING, TurnArbiter


# ── the playout clock ────────────────────────────────────────────────────────


def _adapter():
    """A PlivoWebSocketAdapter with its telephony socket stubbed out."""
    from app.services.plivo_bridge_service import PlivoWebSocketAdapter

    class _Sock:
        def __init__(self):
            self.sent = []

        async def send_text(self, payload):
            self.sent.append(payload)

    a = PlivoWebSocketAdapter(_Sock())
    return a


@pytest.mark.asyncio
async def test_playout_clock_is_zero_on_a_quiet_line():
    assert _adapter().playout_remaining_s() == 0.0


@pytest.mark.asyncio
async def test_silence_extends_the_playout_clock():
    a = _adapter()
    await a.send_silence_ms(400)
    # ~0.4s still to be heard — the send itself returns immediately.
    assert 0.3 < a.playout_remaining_s() <= 0.4


@pytest.mark.asyncio
async def test_queued_audio_accumulates_rather_than_overwrites():
    """Two sends back to back mean the callee hears BOTH — the second doesn't
    reset the clock to its own length."""
    a = _adapter()
    await a.send_silence_ms(300)
    await a.send_silence_ms(300)
    assert a.playout_remaining_s() > 0.5


@pytest.mark.asyncio
async def test_clear_playback_resets_the_clock():
    """A barge-in flush means the callee hears nothing more — so nothing is left
    to wait for. Without this the agent kept talking over the interruption for
    the rest of the queued sentence."""
    a = _adapter()
    await a.send_silence_ms(2000)
    assert a.playout_remaining_s() > 1.0
    await a.clear_playback()
    assert a.playout_remaining_s() == 0.0
    assert any("clearAudio" in s for s in a._ws.sent)


# ── the opener's arbiter lifecycle ───────────────────────────────────────────


class _FakeWS:
    """Websocket + playout clock, recording what the opener emits."""

    def __init__(self, playout_s=0.0):
        self.events = []
        self._left = playout_s
        self.phase_at_first_audio = None

    async def send_json(self, data):
        self.events.append(data)

    def playout_remaining_s(self):
        # Drains a little on each poll so the opener's wait terminates.
        self._left = max(0.0, self._left - 0.15)
        return self._left


@pytest.fixture
def _stub_speech(monkeypatch):
    """No real TTS or session store in a unit test."""
    from app.services.agent_session_store import AgentSessionStore
    from app.services.sarvam_voice_service import SarvamVoiceService

    async def fake_tts(*a, **kw):
        return {}

    async def fake_append(*a, **kw):
        return None

    monkeypatch.setattr(SarvamVoiceService, "stream_sentence_tts", staticmethod(fake_tts))
    monkeypatch.setattr(AgentSessionStore, "append_turn", staticmethod(fake_append))


@pytest.mark.asyncio
async def test_opener_marks_speaking_before_the_first_audio_frame(_stub_speech):
    """A caller who talks over the greeting must be classified as interrupting,
    which requires the arbiter to be SPEAKING by the time audio goes out."""
    from app.services.voice_stream.openers import _play_opener

    arbiter = TurnArbiter()
    ws = _FakeWS()
    seen = {}

    original = ws.send_json

    async def capture(data):
        if data.get("type") == "agent_sentence" and "phase" not in seen:
            seen["phase"] = arbiter.phase
        await original(data)

    ws.send_json = capture

    await _play_opener(
        ws, object(), "[warm]Hi, this is Riya from Acme.[/warm]",
        language="en", call_id="c1", arbiter=arbiter,
    )
    assert seen["phase"] == TURN_SPEAKING


@pytest.mark.asyncio
async def test_opener_holds_speaking_until_the_callee_has_heard_it(_stub_speech):
    """The send returns in milliseconds; the greeting plays for seconds. Marking
    the turn done at send time is what made the arbiter read IDLE mid-greeting."""
    from app.services.voice_stream.openers import _play_opener

    arbiter = TurnArbiter()
    ws = _FakeWS(playout_s=0.6)

    started = asyncio.get_running_loop().time()
    await _play_opener(
        ws, object(), "[warm]Hi, this is Riya.[/warm]",
        language="en", call_id="c1", arbiter=arbiter,
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed >= 0.3           # waited for playback, did not return instantly
    assert arbiter.phase == TURN_DONE


@pytest.mark.asyncio
async def test_opener_sets_and_clears_the_speaking_flag(_stub_speech):
    from app.services.voice_stream.openers import _play_opener

    state = {"speaking": False}
    await _play_opener(
        _FakeWS(), object(), "[warm]Hi there.[/warm]",
        language="en", call_id="c1", turn_state=state,
    )
    assert state["speaking"] is False   # released for the caller's reply


@pytest.mark.asyncio
async def test_opener_still_works_without_an_arbiter(_stub_speech):
    """The web test call and inbound greeting pass neither — back-compat."""
    from app.services.voice_stream.openers import _play_opener

    ws = _FakeWS()
    await _play_opener(ws, object(), "Hello, how can I help?", language="en", call_id="c1")
    assert any(e.get("type") == "agent_sentence" for e in ws.events)
    assert any(e.get("type") == "turn_complete" for e in ws.events)


@pytest.mark.asyncio
async def test_opener_tolerates_a_transport_without_a_playout_clock(_stub_speech):
    """The browser test call's raw WebSocket has no playback queue — the wait is
    reached duck-typed and must simply be skipped."""
    from app.services.voice_stream.openers import _play_opener

    class _Bare:
        def __init__(self):
            self.events = []

        async def send_json(self, data):
            self.events.append(data)

    ws = _Bare()
    arbiter = TurnArbiter()
    await _play_opener(ws, object(), "[warm]Hi.[/warm]", language="en",
                       call_id="c1", arbiter=arbiter)
    assert arbiter.phase == TURN_DONE


@pytest.mark.asyncio
async def test_playout_wait_is_bounded(_stub_speech):
    """A wedged clock must never leave the arbiter believing the agent is
    mid-sentence for the rest of the call."""
    from app.services.voice_stream import openers

    class _Stuck:
        def __init__(self):
            self.events = []

        async def send_json(self, data):
            self.events.append(data)

        def playout_remaining_s(self):
            return 999.0

    monkey = openers._MAX_PLAYOUT_WAIT_S
    openers._MAX_PLAYOUT_WAIT_S = 0.3
    try:
        started = asyncio.get_running_loop().time()
        await openers._await_playout(_Stuck())
        assert asyncio.get_running_loop().time() - started < 1.5
    finally:
        openers._MAX_PLAYOUT_WAIT_S = monkey


def test_opener_listen_window_defaults_to_current_behaviour():
    """APEX_OPENER_LISTEN_MS ships at 0 — the wait-for-hello window is a separate,
    measurable rollout, not a silent behaviour change."""
    from app.core.config import settings

    assert settings.APEX_OPENER_LISTEN_MS == 0
