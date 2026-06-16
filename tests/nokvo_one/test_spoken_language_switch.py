"""Spoken-language switch detection.

Guards the "follow the caller's language" requirement: when the caller simply
STARTS speaking Telugu / Hindi / English (without explicitly asking), the agent
must switch the whole conversation to that language — but a code-switched or
short utterance must NOT flap the reply language.
"""
from __future__ import annotations

from app.services.language_intent import (
    detect_spoken_language_switch,
    script_matches_language,
)


def test_script_matches_language():
    assert script_matches_language("ధర ఎంత అండి", "te")
    assert script_matches_language("क्या कीमत है", "hi")
    assert script_matches_language("what is the price", "en")
    # English label but native script present → not English.
    assert not script_matches_language("ధర ఎంత", "en")
    # Telugu label but pure Latin → script doesn't back the label.
    assert not script_matches_language("what is the price", "te")
    # Wrong native script (Devanagari text labelled Telugu).
    assert not script_matches_language("क्या कीमत है", "te")


def test_switch_to_native_when_caller_starts_speaking_telugu():
    # Call locked to English; caller speaks a full Telugu sentence.
    assert (
        detect_spoken_language_switch("నాకు ఆ project details కావాలి", "te", "en")
        == "te"
    )


def test_switch_to_english_needs_a_longer_run():
    # Locked Telugu, a short English courtesy must NOT flip the call.
    assert detect_spoken_language_switch("yes please", "en", "te") is None
    # A full English sentence does switch.
    assert (
        detect_spoken_language_switch(
            "can you send me the brochure please", "en", "te"
        )
        == "en"
    )


def test_no_switch_on_short_acks_or_numbers():
    assert detect_spoken_language_switch("OK", "en", "te") is None
    assert detect_spoken_language_switch("సరే", "te", "en") is None  # 1 word


def test_loanword_heavy_native_sentence_does_not_flip_to_english():
    # Telugu sentence (native script) that STT mislabels as English because of
    # the English loanwords — script check keeps the call in Telugu.
    text = "ఆ project లో WhatsApp లో details పంపండి"
    assert detect_spoken_language_switch(text, "en", "te") is None


def test_no_switch_when_detected_equals_current():
    assert detect_spoken_language_switch("నాకు details కావాలి", "te", "te") is None


def test_confidence_floor_blocks_low_confidence_switch():
    text = "నాకు ఆ project details కావాలి"
    assert detect_spoken_language_switch(text, "te", "en", confidence=0.2) is None
    assert detect_spoken_language_switch(text, "te", "en", confidence=0.9) == "te"


def test_switch_between_two_native_languages():
    # Locked Hindi, caller speaks Telugu.
    assert (
        detect_spoken_language_switch("నాకు ఆ details కావాలి", "te", "hi") == "te"
    )
