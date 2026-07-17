"""Conversation-style voice overlay (prosody.style_prosody / prosody_for).

Selecting an APEX conversation style adjusts the agent's voice, not just its
wording: the style baseline composes with the per-tone prosody (pace
multiplies, pitch adds, loudness multiplies). Styles without an overlay —
scripted / professional / unknown — must be byte-identical to the unstyled
path so their warmed TTS cache keys never rotate.
"""
from __future__ import annotations

from app.services.prosody import known_tones, prosody_for, style_prosody


def test_no_overlay_styles_are_identity():
    for style in (None, "", "scripted", "professional", "nonsense"):
        assert style_prosody(style) is None
        for tone in known_tones():
            assert prosody_for(tone, style) == prosody_for(tone)


def test_overlay_composes_with_tone():
    for style in ("human", "luxury", "friendly"):
        overlay = style_prosody(style)
        assert overlay is not None
        for tone in known_tones():
            base = prosody_for(tone)
            styled = prosody_for(tone, style)
            assert styled.pace == max(0.3, min(3.0, base.pace * overlay.pace))
            assert styled.pitch == max(-0.75, min(0.75, base.pitch + overlay.pitch))
            assert styled.loudness == max(0.1, min(3.0, base.loudness * overlay.loudness))


def test_style_lookup_is_case_and_whitespace_tolerant():
    assert prosody_for("warm", "  LUXURY ") == prosody_for("warm", "luxury")


def test_styles_stay_within_sarvam_ranges():
    """Composed values must always be speakable: pace 0.3–3.0, pitch
    -0.75–0.75, loudness 0.1–3.0 (the Bulbul accepted ranges)."""
    for style in ("human", "luxury", "friendly", None):
        for tone in known_tones():
            p = prosody_for(tone, style)
            assert 0.3 <= p.pace <= 3.0
            assert -0.75 <= p.pitch <= 0.75
            assert 0.1 <= p.loudness <= 3.0
