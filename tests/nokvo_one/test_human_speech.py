"""Subtle human-speech delivery.

The agent emits sparse natural fillers ("Um,", "Well,") wrapped in
`[thinking]…[/thinking]`; the prosody pipeline renders that segment with a
slightly slower pace (Sarvam Bulbul has no elongation markup, so pace + a
trailing comma/ellipsis is the lever). The fillers themselves are plain text,
so they're spoken; the tone tags are stripped.
"""
from __future__ import annotations

import asyncio

from app.services.prosody import (
    DEFAULT_TONE,
    known_tones,
    prosody_for,
    stream_prosody_chunks,
)


def test_thinking_tone_is_slower_and_registered():
    assert "thinking" in known_tones()
    # Slower than the neutral baseline = the drawn-out feel of a hesitation.
    assert prosody_for("thinking").pace < prosody_for(DEFAULT_TONE).pace


def test_thinking_tag_parses_and_filler_text_is_preserved():
    async def _gen():
        for tok in ["[thinking]", "Um, ", "let me see.", "[/thinking]",
                    " [neutral]It's ready.[/neutral]"]:
            yield tok

    async def _run():
        return [(c.text, c.tone) async for c in stream_prosody_chunks(_gen())]

    chunks = asyncio.new_event_loop().run_until_complete(_run())
    tones = [t for _, t in chunks]
    joined = " ".join(txt for txt, _ in chunks).lower()

    assert "thinking" in tones                 # the hesitation chunk got the slow tone
    assert "um" in joined and "let me see" in joined   # the filler is spoken
    assert "[thinking]" not in joined          # tone tags are stripped before TTS
