"""Adaptive end-of-utterance tiering (`_eou_completeness_tier`).

The classifier decides how long to wait for silence before firing a turn:
- **fast** (~450ms): high-confidence-complete — questions, short yes/no, pure
  time/day answers. The common "answering the agent" turn.
- **neutral** (~700ms): ambiguous declaratives — room for a self-correction
  ("…actually Sunday") to land before we fire.
- **continuation** (~2300ms): speech trailing off — never cut the caller off.

Quality guard cases from review: Hinglish particles ("two BHK na") must NOT be
read as continuation, and bare declaratives with STT punctuation ("Saturday
works.") must NOT fast-fire.
"""
from __future__ import annotations

import pytest

from app.services.nokvo_one_voice_stream_service import _eou_completeness_tier as tier


@pytest.mark.parametrize("text", [
    "10 AM",
    "10:30",
    "tomorrow at 4",
    "Saturday morning",
    "this weekend",
    "Saturday only",            # trailing Hinglish particle stripped → pure day
    "yes",
    "yeah sure",
    "no thanks",
    "haan",                     # Hinglish affirmation
    "what is the price of the 3 BHK?",   # real question mark = reliable complete
    "where is it?",             # '?' overrides the trailing function word "it"
    "thank you",                # closer
    "Alright, thank you.",      # closer (scan finding T8 — was over-waited)
    "thanks",                   # closer
])
def test_fast_tier(text):
    assert tier(text) == "fast", text


@pytest.mark.parametrize("text", [
    "Saturday works.",          # bare declarative + STT period → NOT fast
    "two BHK na",               # particle stripped → "two BHK" (not a 2300ms wait)
    "Skyline Heights",          # noun phrase
    "the price is reasonable",  # declarative ending on a content word
    # Scan finding T1: a COMPLETE utterance ending in a weak aux ("have") after
    # substantial content must NOT over-wait at 2300ms — it's neutral, not continuation.
    "Yeah, I just wanted to know about all the projects that you guys have",
]
)
def test_neutral_tier(text):
    assert tier(text) == "neutral", text


@pytest.mark.parametrize("text", [
    "I want to",                # trailing infinitive
    "so that you would",        # trailing modal
    "it is in",                 # trailing preposition
    "um",                       # filler
    "my number is 98",          # mid-number dictation (context word present)
    "my phone is 7569",         # mid-number dictation
    "I have",                   # SHORT weak-tail fragment → still waits (safe)
    "do you",                   # SHORT weak-tail fragment → still waits (safe)
    # Field call 072f59e6: a trailing pronoun contraction is mid-thought — judge
    # it on its root pronoun so the caller isn't cut off ("uh, it's … 1.8 crore").
    "uh, it's",
    "it's",
    "I'm",
    "that's",
])
def test_continuation_tier(text):
    assert tier(text) == "continuation", text


@pytest.mark.parametrize("text", [
    # NEGATIVE contractions end a complete reply far more often than they trail
    # off — they must NOT inherit the long pronoun-contraction wait.
    "I don't",
    "I can't",
    "she isn't",
])
def test_negative_contractions_do_not_overwait(text):
    assert tier(text) != "continuation", text


def test_empty_is_neutral():
    assert tier("") == "neutral"
    assert tier("   ") == "neutral"


def test_question_mark_beats_trailing_function_word():
    # "?" is the reliable signal; a trailing continuation word must not win.
    assert tier("and how much is it?") == "fast"


def test_period_does_not_fast_fire():
    # STT inserts '.' on pauses — a bare declarative with '.' stays neutral so a
    # self-correction can still land.
    assert tier("Saturday works.") == "neutral"
    assert tier("that sounds good.") == "neutral"
