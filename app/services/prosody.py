"""Tone-driven prosody for voice replies.

The LLM emits inline tone tags around each segment of its reply, e.g.::

    [empathy]Oh, that's frustrating.[/empathy] [question]What's your order number?[/question]

We parse the stream incrementally (so audio can start as soon as the first
chunk is ready), strip the tags from the text that goes to TTS, and look up
the matching pace/pitch/loudness for each tagged chunk. The result is that
each sentence is synthesized with prosody appropriate for what it's saying —
empathy is slower and softer, excitement is brisker, questions trail down
slightly, etc.

Sarvam Bulbul accepts ``pace`` (0.3–3.0), ``pitch`` (-0.75–0.75),
``loudness`` (0.1–3.0). The deltas here are conservative so the agent
doesn't sound cartoonish.

Tone vocabulary (deliberately small — every extra tag risks confusing the
model and stealing attention from content):

    empathy   apologies, bad news, "I'm sorry to hear that"
    warm      greetings, acknowledgments, "of course", "got it"
    neutral   facts, policies, statements — DEFAULT
    excited   good news, enthusiasm
    question  direct questions
    thinking  hesitations / thinking aloud ("um…", "well…") — slightly slower

Ported from agent_lab/voice-rag-agent/orchestrator/app/llm/prosody.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass(slots=True, frozen=True)
class Prosody:
    pace: float
    pitch: float
    loudness: float


# Conservative deltas around the neutral baseline. Pushing further sounds artificial.
# NOTE bulbul:v3 accepts ONLY pace (pitch/loudness are stripped for it in
# sarvam_voice_service), so on the current default model pace IS the prosody —
# hence deltas wide enough to be audible. CACHE WARNING: pace lands in the TTS
# byte-cache key, so changing any value here rotates the key of every warmed
# scripted line — run scripts/apex_reprewarm.py right after deploying a change.
_PROSODY: dict[str, Prosody] = {
    "empathy":  Prosody(pace=0.92, pitch=-0.10, loudness=1.0),
    "warm":     Prosody(pace=1.0,  pitch= 0.05, loudness=1.0),
    "neutral":  Prosody(pace=1.0,  pitch= 0.0,  loudness=1.0),
    "excited":  Prosody(pace=1.08, pitch= 0.10, loudness=1.0),
    # Questions land measurably slower than statements in natural speech —
    # 0.97 was inaudible on v3; 0.94 reads as a person actually asking.
    "question": Prosody(pace=0.94, pitch= 0.05, loudness=1.0),
    # Hesitation / thinking-aloud: a slightly slower pace gives the natural
    # drawn-out feel of a soft filler ("um…", "well…") without letter-repetition
    # (Bulbul has no elongation markup; pace + a trailing comma/… is the lever).
    "thinking": Prosody(pace=0.88, pitch= 0.0,  loudness=1.0),
}

DEFAULT_TONE = "neutral"


def prosody_for(tone: str) -> Prosody:
    return _PROSODY.get(tone, _PROSODY[DEFAULT_TONE])


def known_tones() -> list[str]:
    return list(_PROSODY.keys())


# [tone] and [/tone]. Tone names are lowercase ASCII only.
_TAG_OPEN = re.compile(r"\[([a-z]+)\]")
_TAG_CLOSE = re.compile(r"\[/([a-z]+)\]")
# End of a sentence (period/!/?/danda) followed by whitespace or EOS.
# A "." only terminates when NOT preceded by a digit, so a decimal — even one
# the model spaced out, e.g. "₹2. 45Cr" — is never split across TTS chunks
# (which made the Telugu voice read "₹2." as "rendu rupailu"). ! ? । always end.
_SENTENCE_END = re.compile(r"(?:(?<!\d)\.|[!?।])(?:\s+|$)")


@dataclass(slots=True)
class ProsodyChunk:
    text: str
    tone: str


def strip_tone_tags(text: str) -> str:
    """Remove any prosody tags from a string. For sanitizing cached/non-streamed answers."""
    text = _TAG_OPEN.sub("", text)
    text = _TAG_CLOSE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


async def stream_prosody_chunks(
    token_stream: AsyncIterator[str],
) -> AsyncIterator[ProsodyChunk]:
    """Parse an incoming LLM token stream into prosody chunks.

    Emits a chunk whenever:
      - a sentence terminator is seen (yields buffered text with the current tone)
      - a closing tag is seen (yields any prefix with the current tone, then resets to neutral)
      - an opening tag is seen (yields buffered text with the prior tone, switches tone)

    Unknown tags fall back to neutral and are stripped from output text either way.
    """
    buf = ""
    current_tone = DEFAULT_TONE

    def _take(end: int) -> str:
        nonlocal buf
        text = buf[:end].strip()
        buf = buf[end:]
        return text

    async for delta in token_stream:
        if not delta:
            continue
        buf += delta

        while True:
            m_open = _TAG_OPEN.search(buf)
            m_close = _TAG_CLOSE.search(buf)
            m_end = _SENTENCE_END.search(buf)

            candidates: list[tuple[re.Match[str], str]] = []
            if m_open:
                candidates.append((m_open, "open"))
            if m_close:
                candidates.append((m_close, "close"))
            if m_end:
                candidates.append((m_end, "end"))
            if not candidates:
                break

            event_match, event_kind = min(candidates, key=lambda c: c[0].start())

            if event_kind == "open":
                tag_len = event_match.end() - event_match.start()
                prefix = _take(event_match.start())
                if prefix:
                    yield ProsodyChunk(text=prefix, tone=current_tone)
                buf = buf[tag_len:]
                tone = event_match.group(1).lower()
                current_tone = tone if tone in _PROSODY else DEFAULT_TONE
            elif event_kind == "close":
                tag_len = event_match.end() - event_match.start()
                prefix = _take(event_match.start())
                if prefix:
                    yield ProsodyChunk(text=prefix, tone=current_tone)
                buf = buf[tag_len:]
                current_tone = DEFAULT_TONE
            else:  # sentence terminator — include it so TTS handles punctuation
                sent = _take(event_match.end())
                if sent:
                    yield ProsodyChunk(text=sent, tone=current_tone)

    if buf.strip():
        yield ProsodyChunk(text=buf.strip(), tone=current_tone)
