"""Decimal-aware sentence splitting.

A rupee/decimal amount must never be split across TTS chunks. The model
sometimes emits "₹2. 45Cr" (a space after the decimal point); the sentence
splitters used to break that at ". " into "₹2." + "45Cr", and the per-chunk
TTS normalizer then read "₹2." as "2 rupees" → Telugu "rendu rupailu".
Both `prosody.stream_prosody_chunks` (live path) and the pipeline's
`_first_sentence` must keep the amount whole; real sentence ends still split.
"""
from __future__ import annotations

import asyncio

from app.services.prosody import stream_prosody_chunks
from app.services.nokvo_one_voice_pipeline import _first_sentence
from app.services.sarvam_voice_service import normalize_text_for_tts


def _chunks(tokens):
    async def _gen():
        for t in tokens:
            yield t

    async def _run():
        return [c async for c in stream_prosody_chunks(_gen())]

    return asyncio.new_event_loop().run_until_complete(_run())


def test_prosody_keeps_spaced_decimal_in_one_chunk():
    chunks = _chunks(
        ["[neutral]price roughly ₹2. 45Cr ", "nunchi start avuthundi. ", "Meeru chudali sir?[/neutral]"]
    )
    texts = [c.text for c in chunks]
    # The amount must not be orphaned in its own chunk / end a chunk at "₹2."
    assert not any(t.endswith("₹2.") or t.strip() == "₹2." for t in texts)
    amount_chunk = next(t for t in texts if "45Cr" in t)
    assert "₹2. 45Cr" in amount_chunk or "₹2.45Cr" in amount_chunk
    # The real sentence end (?) still splits.
    assert any(t.strip() == "Meeru chudali sir?" for t in texts)


def test_prosody_still_splits_real_sentence_ends():
    chunks = _chunks(["[neutral]Manchi project undi. ", "Meeru site visit ki vasthara?[/neutral]"])
    texts = [c.text.strip() for c in chunks]
    assert "Manchi project undi." in texts
    assert "Meeru site visit ki vasthara?" in texts


def test_first_sentence_does_not_split_decimal():
    out = _first_sentence("price roughly ₹2. 45Cr nunchi start. Next sentence here please.")
    assert out is not None
    first, rest = out
    assert "₹2. 45Cr" in first        # amount stayed in the first sentence
    assert first.endswith("start.")
    assert rest == "Next sentence here please."


def test_first_sentence_normal_period_still_splits():
    out = _first_sentence("It costs a lot today. Tomorrow we visit.")
    assert out == ("It costs a lot today.", "Tomorrow we visit.")


def test_amount_chunk_normalizes_to_spoken_form():
    # End-to-end: the whole-amount chunk → spoken rupee amount, no bare "₹2".
    spoken = normalize_text_for_tts("price roughly ₹2. 45Cr nunchi start avuthundi.")
    assert "two point four five crore rupees" in spoken
    assert "₹2" not in spoken
