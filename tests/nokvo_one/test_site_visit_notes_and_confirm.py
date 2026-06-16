"""Inbound site-visit deterministic call notes + templated booking confirmation.

Covers the two fixes:
  1. Every inbound site-visit / lead record carries a deterministic call note
     written synchronously at creation (so a flaky post-call LLM condenser can
     never leave the record noteless) — `_deterministic_call_note`.
  2. The booking-confirmation turn is a clean per-language template instead of
     corruptible free-generated Telugu/Hindi — `_site_visit_confirm_text` +
     `_is_site_visit_confirmation_turn`.
"""
from __future__ import annotations

import re

from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.nokvo_one_voice_stream_service import (
    _is_site_visit_confirmation_turn,
    _site_visit_confirm_text,
)
from app.services.voice_turn_policy import (
    extract_datetime_phrase,
    text_has_datetime,
    text_is_question,
)

# Telugu / Devanagari script ranges — used to assert a template is genuinely
# native-script (only allow-listed English loanwords + digits stay Latin).
_TELUGU = re.compile(r"[ఀ-౿]")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Loanwords/units we deliberately keep in English mid-sentence.
_ALLOWED_LATIN = {"note", "team", "sms", "confirm", "am", "pm", "tomorrow"}


def _stray_latin_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z]+", text) if w.lower() not in _ALLOWED_LATIN]


# ── date/time helpers ────────────────────────────────────────────────────────


def test_text_has_datetime_handles_telugu_relative():
    assert text_has_datetime("రేపు 10 గంటలకి")          # Telugu "tomorrow"
    assert text_has_datetime("tomorrow at 10 AM")
    assert text_has_datetime("Saturday")
    assert not text_has_datetime("మీ projects చెప్పండి")  # no date/time


def test_extract_datetime_phrase():
    assert extract_datetime_phrase("tomorrow at 10 AM") == "tomorrow 10 AM"
    # Telugu clock-hour ("10 గంటలకి") is now captured as a concrete time.
    assert extract_datetime_phrase("రేపు 10 గంటలకి") == "tomorrow 10:00"
    assert extract_datetime_phrase("మీ projects చెప్పండి") == ""


def test_text_is_question():
    assert text_is_question("Site visit ఎంత cost అవుతుంది?")
    assert text_is_question("what time works?")
    assert not text_is_question("రేపు 10 గంటలకి")


# ── booking-confirmation trigger ─────────────────────────────────────────────


def _history_with_visit_agreement():
    return [
        {"role": "user", "content": "మీ దగ్గర ఉన్న projects చెప్పండి"},
        {"role": "assistant", "content": "Skyline Heights, Tukkuguda ..."},
        {"role": "user", "content": "Site visit kastha?"},
        {"role": "assistant", "content": "ఏ రోజుకి వస్తారో చెప్పగలరా?"},
    ]


def test_confirmation_trigger_fires_after_agreement_and_datetime():
    assert _is_site_visit_confirmation_turn(
        "రేపు 10 గంటలకి", _history_with_visit_agreement()
    )


def test_confirmation_trigger_skips_question_turn():
    assert not _is_site_visit_confirmation_turn(
        "రేపు రావచ్చా, cost ఎంత?", _history_with_visit_agreement()
    )


def test_confirmation_trigger_skips_when_no_datetime():
    assert not _is_site_visit_confirmation_turn(
        "సరే అండి", _history_with_visit_agreement()
    )


def test_confirmation_trigger_skips_without_prior_visit_agreement():
    # Date/time given, but the caller never signalled a site-visit.
    history = [
        {"role": "user", "content": "price ఎంత?"},
        {"role": "assistant", "content": "₹2.45Cr andi."},
    ]
    assert not _is_site_visit_confirmation_turn("రేపు 10 గంటలకి", history)


# ── confirmation template ────────────────────────────────────────────────────


def test_confirm_text_is_native_script_telugu():
    out = _site_visit_confirm_text("te", "10 AM")
    assert _TELUGU.search(out)                 # genuinely Telugu script
    assert "10 AM" in out                      # date/time stays Latin/digits
    assert not _stray_latin_words(out)         # no corrupted/stray Latin words


def test_confirm_text_is_native_script_hindi():
    out = _site_visit_confirm_text("hi", "tomorrow")
    assert _DEVANAGARI.search(out)
    assert not _stray_latin_words(out)


def test_confirm_text_english_and_no_when_fallback():
    assert "tomorrow 10 AM" in _site_visit_confirm_text("en", "tomorrow 10 AM")
    # No date/time → generic but still complete.
    assert _site_visit_confirm_text("te", "")
    assert _site_visit_confirm_text("en", "").endswith("shortly.")


# ── deterministic call note ──────────────────────────────────────────────────


def test_deterministic_note_site_visit_includes_datetime_and_contact():
    note = NokvoOneVoicePipeline._deterministic_call_note(
        kind="site_visit",
        name="Nihar",
        ani="+919876543210",
        memory={"bhk": "3 BHK", "location_preference": "Gachibowli"},
        history=[{"role": "user", "content": "రేపు 10 గంటలకి"}],
    )
    assert note.startswith("Caller agreed to a site visit.")
    assert "tomorrow" in note          # Telugu relative date normalised
    assert "10:00" in note             # Telugu clock-hour captured as a time
    assert "3 BHK" in note
    assert "Gachibowli" in note
    assert "Nihar" in note
    assert "+919876543210" in note


def test_deterministic_note_lead_kind():
    note = NokvoOneVoicePipeline._deterministic_call_note(
        kind="lead",
        name="Asha",
        ani=None,
        memory={"requested_info": "brochure, pricing"},
        history=[],
    )
    assert note.startswith("Caller enquired about properties.")
    assert "brochure, pricing" in note
    assert "Asha" in note


def test_deterministic_note_is_never_empty_with_no_facts():
    note = NokvoOneVoicePipeline._deterministic_call_note(
        kind="site_visit", name=None, ani=None, memory={}, history=[],
    )
    assert note.strip()  # always at least the intro sentence
