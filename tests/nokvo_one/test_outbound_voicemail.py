"""Outbound answering-machine guard.

All four most-recent LangSmith outbound calls reached voicemail and the agent
pitched to the machine (and the greeting's STT kept barging the opener). The
guard detects the greeting from its transcript, leaves one short message, and
hangs up. These tests lock in the detector + message rendering — the *strong*
phrase list must fire on real voicemail greetings and stay quiet on a human.
"""
from app.services.nokvo_one_voice_stream_service import (
    _is_voicemail_utterance,
    _voicemail_message,
)


# The exact strings Sarvam STT produced on the voicemail calls (from LangSmith).
VOICEMAIL_GREETINGS = [
    "Call has been forwarded to voicemail The person you are trying to reach is "
    "not available. had the tone",
    "Please record your message.",
    "When you have finished recording you may hang up",
    "being forwarded to voicemail. The person you are you are trying to reach is "
    "not available",
    "you have reached the voicemail of 9 8 4 ...",
    "please leave your message after the tone",
]

HUMAN_TURNS = [
    "Hello",
    "Yeah, I can speak right now.",
    "3 BHK BHK",
    "I'm looking for self use",
    "Sorry, who is this?",
    "",
]


def test_detects_real_voicemail_greetings():
    for greeting in VOICEMAIL_GREETINGS:
        assert _is_voicemail_utterance(greeting) is True, greeting


def test_does_not_false_trigger_on_humans():
    for turn in HUMAN_TURNS:
        assert _is_voicemail_utterance(turn) is False, turn


def test_detection_is_case_insensitive():
    assert _is_voicemail_utterance("PLEASE RECORD YOUR MESSAGE") is True


def test_message_personalises_english():
    line = _voicemail_message("en", caller_name="Riya", company_name="My Home")
    assert "Riya" in line and "My Home" in line
    assert "call us back" in line.lower()


def test_message_handles_missing_names():
    line = _voicemail_message("en", caller_name="", company_name="")
    assert line.strip()  # never empty — always something to leave


def test_message_native_script_for_te_hi():
    te = _voicemail_message("te", caller_name="Riya", company_name="My Home")
    hi = _voicemail_message("hi", caller_name="Riya", company_name="My Home")
    # Telugu / Devanagari native script present (so Sarvam TTS says it right).
    assert any("ఀ" <= ch <= "౿" for ch in te)
    assert any("ऀ" <= ch <= "ॿ" for ch in hi)
