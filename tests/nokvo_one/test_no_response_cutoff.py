"""No-response guardrail: a picked-up caller who stays silent through a nudge
gets a brief goodbye + hang up (the nudge→cut escalation ladder).

Component-level tests — the silence WATCHDOG timer, the per-language goodbye line,
and the CUT helper (speaks then closes the WS). The ladder wiring itself lives in
run_session's closure and is covered by these pieces + the manual staging call.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.agent_outbound_context import (
    ProactiveSilenceWatchdog,
    _DEFAULT_SILENCE_TIMEOUT_SECONDS,
)
from app.services.agent_session_store import AgentSessionStore
from app.services.nokvo_one_voice_stream_service import (
    NokvoOneVoiceStreamService,
    _no_response_goodbye_text,
)
from app.services.sarvam_voice_service import SarvamVoiceService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


# ── silence window default ──────────────────────────────────────────────────

def test_silence_window_default_is_10s():
    assert _DEFAULT_SILENCE_TIMEOUT_SECONDS == 10.0


# ── per-language goodbye line ───────────────────────────────────────────────

def test_no_response_goodbye_text_all_languages():
    en = _no_response_goodbye_text("en")
    hi = _no_response_goodbye_text("hi")
    te = _no_response_goodbye_text("te")
    assert en.strip() and hi.strip() and te.strip()
    assert any("ऀ" <= c <= "ॿ" for c in hi)  # Devanagari
    assert any("ఀ" <= c <= "౿" for c in te)  # Telugu
    # frames it as "not a good time", not disinterest
    assert "good time" in en.lower()
    # BCP-47 tags degrade to the base language, unknown → English
    assert _no_response_goodbye_text("hi-IN") == hi
    assert _no_response_goodbye_text(None) == en


# ── watchdog timer ──────────────────────────────────────────────────────────

def test_watchdog_fires_after_timeout():
    fired = {"n": 0}

    async def on_fire():
        fired["n"] += 1

    async def run():
        wd = ProactiveSilenceWatchdog(timeout_seconds=0.5, on_fire=on_fire)  # floored at 0.5
        wd.arm()
        await asyncio.sleep(0.7)

    _run(run())
    assert fired["n"] == 1


def test_watchdog_cancel_suppresses_fire():
    fired = {"n": 0}

    async def on_fire():
        fired["n"] += 1

    async def run():
        wd = ProactiveSilenceWatchdog(timeout_seconds=0.5, on_fire=on_fire)
        wd.arm()
        await asyncio.sleep(0.05)
        wd.cancel()  # caller spoke → suppress
        await asyncio.sleep(0.7)

    _run(run())
    assert fired["n"] == 0


# ── cut helper: speaks goodbye then closes the WS ───────────────────────────

def test_end_call_no_response_speaks_and_closes(monkeypatch):
    async def fake_tts(*args, **kwargs):
        return {}

    async def fake_append(*args, **kwargs):
        return None

    monkeypatch.setattr(SarvamVoiceService, "stream_sentence_tts", staticmethod(fake_tts))
    monkeypatch.setattr(AgentSessionStore, "append_turn", staticmethod(fake_append))

    ws = _FakeWS()
    turn_state = {"speaking": False}
    _run(
        NokvoOneVoiceStreamService._end_call_no_response(
            ws,
            SimpleNamespace(provider_status={}),
            language="en",
            call_id="c1",
            campaign_context={},
            arbiter=None,
            turn_state=turn_state,
        )
    )
    goodbye = [m for m in ws.sent if m.get("source") == "no_response_end"]
    assert goodbye and goodbye[0]["sentence"].strip()
    assert ws.closed is True           # call was cut
    assert turn_state["speaking"] is True  # marked speaking so nothing barges the final line


def test_end_call_no_response_hangs_up_even_if_tts_fails(monkeypatch):
    async def boom_tts(*args, **kwargs):
        raise RuntimeError("sarvam down")

    async def fake_append(*args, **kwargs):
        return None

    monkeypatch.setattr(SarvamVoiceService, "stream_sentence_tts", staticmethod(boom_tts))
    monkeypatch.setattr(AgentSessionStore, "append_turn", staticmethod(fake_append))

    ws = _FakeWS()
    _run(
        NokvoOneVoiceStreamService._end_call_no_response(
            ws, SimpleNamespace(provider_status={}), language="en", call_id="c1",
            campaign_context={}, arbiter=None, turn_state={"speaking": False},
        )
    )
    assert ws.closed is True  # a TTS hiccup must still hang up
