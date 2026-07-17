"""Deterministic utterance classifiers: check-ins, backchannels, voicemail
greetings, and real-estate inventory / site-visit detection.

Extracted from nokvo_one_voice_stream_service.py (which re-exports every
name here, so existing imports keep working). Byte-verbatim move — no
behavior change.
"""
from __future__ import annotations

import re


# Short utterances the user produces while the agent is still composing the
# previous answer. We acknowledge with a short "yes, one moment…" instead of
# treating these as barge-ins — the pending answer continues unmodified.
_CHECK_IN_EXACT = {
    "hello", "hello?", "hellooo", "hi", "hi?", "hey", "hey?",
    "hello hello", "hello are you there", "are you there",
    "are you there?", "you there", "you there?",
    "still there", "still there?", "you still there",
    "anyone there", "anyone there?", "can you hear me",
    "can you hear me?", "you hear me", "you listening",
    "are you listening", "hello can you hear me",
    "हलो", "हैलो", "हेलो", "हैलो?", "क्या आप हैं",
    "क्या आप वहाँ हैं", "हो क्या", "क्या आप सुन रहे हैं",
    "హలో", "హలో?", "ఉన్నారా", "మీరు ఉన్నారా",
    "ஹலோ", "இருக்கீங்களா", "கேக்குதா",
}
_CHECK_IN_CONTAINS = (
    "are you there",
    "you still there",
    "can you hear me",
    "are you listening",
    "anyone there",
    "क्या आप हैं",
    "सुन रहे हैं",
    "మీరు ఉన్నారా",
    "இருக்கீங்களா",
)

# Short backchannels / acknowledgements a caller drops WHILE the agent is
# speaking — "uh-huh", "yeah", "haan", "avunu". On an OUTBOUND call these must
# NOT cancel the agent (it's affirmation, not a turn-grab). Mirrors the
# _CHECK_IN_* lists; used by the transcript-level barge-in backstop. The primary
# outbound guard is the time-based sustained-speech window (see the streaming
# speech_start path) — this list only catches the vad_blob transcript path.
_BACKCHANNEL_WORDS = {
    "uh-huh", "uhhuh", "uh huh", "mhm", "mm", "mmm", "hmm", "mm-hmm", "mmhmm",
    "yeah", "yep", "yup", "ok", "okay", "k", "right", "sure", "got it",
    "gotcha", "i see", "yes", "ya", "aha", "oh", "cool", "nice", "alright",
    # Hindi
    "haan", "हाँ", "हां", "ji", "जी", "achha", "अच्छा", "theek", "ठीक",
    "theek hai", "ठीक है", "sahi", "सही", "हम्म",
    # Telugu
    "avunu", "అవును", "sare", "సరే", "ఊ", "ఆ", "అవ్",
    # Tamil
    "ஆமா", "சரி", "ஆம்",
}


def _is_backchannel_utterance(text: str) -> bool:
    """True when ``text`` is a short backchannel/acknowledgement (≤2 words and in
    :data:`_BACKCHANNEL_WORDS`) or empty — i.e. the kind of "uh-huh" / cough a
    caller emits while the agent is talking, which should NOT count as a
    barge-in on an outbound call."""
    cleaned = " ".join((text or "").lower().split()).rstrip("?.,!।؟…")
    if not cleaned:
        return True
    if len(cleaned.split()) > 2:
        return False
    return cleaned in _BACKCHANNEL_WORDS


def _is_check_in_utterance(text: str) -> bool:
    cleaned = " ".join((text or "").lower().split())
    cleaned = cleaned.rstrip("?.,!।؟")
    if not cleaned:
        return False
    if len(cleaned.split()) > 6:
        return False
    if cleaned in _CHECK_IN_EXACT:
        return True
    return any(needle in cleaned for needle in _CHECK_IN_CONTAINS)


# Answering-machine / voicemail greetings, as Sarvam STT transcribes them. When
# an OUTBOUND call hits voicemail, the machine's greeting gets transcribed as a
# "caller" turn — the agent must NOT pitch to a recording. These are deliberately
# *strong*, specific phrases so a live human's words don't false-trigger the
# guard. Matched case-insensitively as substrings; outbound-only.
_VOICEMAIL_PHRASES = (
    "forwarded to voicemail",
    "reached the voicemail",
    "your call has been forwarded",
    "leave a message",
    "leave your message",
    "record your message",
    "please record",
    "after the tone",
    "after the beep",
    "the person you are trying to reach",
    "not available to take your call",
    "when you have finished recording",
    "is unavailable",
)


def _is_voicemail_utterance(text: str | None) -> bool:
    """True when ``text`` looks like an answering-machine greeting (outbound only).
    Pure + unit-testable."""
    if not text:
        return False
    low = text.casefold()
    return any(phrase in low for phrase in _VOICEMAIL_PHRASES)


_INV_NOUN = r"(projects?|propert(?:y|ies)|listings?|inventory|options?)"


def _is_project_inventory_question(text: str) -> bool:
    """True when the caller is asking WHICH projects/properties exist (an
    inventory listing), e.g. "what projects do you have", "which properties",
    "what do you guys offer". Deliberately tight: it must not fire when the
    caller names a specific project or asks to book/visit one (those stay on
    the LLM/booking path). Callers code-switch heavily, so the English
    "project/property" token is the anchor even in hi/te utterances."""
    t = (text or "").lower().strip()
    if not t:
        return False
    # Booking / specific-project / brochure intents are NOT inventory listings.
    if re.search(r"\b(book|schedule|site\s*visit|brochure for|come (?:down|over)|claim)\b", t):
        return False
    if re.search(rf"\b(what|which|any|list|show me|tell me|name|kaun\s*se|kitne|enni|em(?:i)?)\b.{{0,30}}\b{_INV_NOUN}\b", t):
        return True
    if re.search(rf"\b{_INV_NOUN}\b.{{0,30}}\b(do (?:you|u) have|you guys have|available|right now|currently|on offer|you offer)\b", t):
        return True
    if re.search(r"\bwhat (?:do|are|kind of|all)\b.{0,20}\b(?:you|u|you guys|u guys)\b.{0,20}\b(?:have|offer|got|selling|building)\b", t):
        return True
    return False


def _is_site_visit_confirmation_turn(text: str, history: list[dict[str, str]]) -> bool:
    """Conservative trigger for the templated booking confirmation: the caller
    just stated a date/time, isn't asking a question, and visit intent was
    established earlier in the call. Narrow on purpose — when it doesn't fire,
    the normal LLM path runs."""
    from app.services.tool_flow_policy import caller_agreed_to_site_visit
    from app.services.voice_turn_policy import text_has_datetime, text_is_question

    if not text or text_is_question(text):
        return False
    if not text_has_datetime(text):
        return False
    convo = list(history or []) + [{"role": "user", "content": text}]
    return caller_agreed_to_site_visit(convo)
