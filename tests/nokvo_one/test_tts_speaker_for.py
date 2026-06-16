"""SarvamVoiceService.tts_speaker_for — per-language native TTS speaker.

A single global speaker makes Telugu speak in the Hindi-leaning default voice.
These lock the selection precedence: tenant override → per-language default →
global default, so Telugu/Hindi can each use a native-sounding voice while
behaviour is unchanged until the per-language overrides are populated.
"""
from __future__ import annotations

from app.core.config import settings
from app.services.sarvam_voice_service import SarvamVoiceService as S


def _base(monkeypatch, *, te="", hi="", default="shubh"):
    monkeypatch.setattr(settings, "SARVAM_TTS_SPEAKER", default)
    monkeypatch.setattr(settings, "SARVAM_TTS_SPEAKER_TE", te)
    monkeypatch.setattr(settings, "SARVAM_TTS_SPEAKER_HI", hi)
    monkeypatch.setattr(settings, "SARVAM_TTS_SPEAKER_EN", "")


def test_unset_falls_back_to_global_default_for_all_languages(monkeypatch):
    _base(monkeypatch)
    for lang in ("te", "hi", "en", "ta", None, "unknown"):
        assert S.tts_speaker_for(lang, {}) == "shubh"


def test_per_language_env_default_used(monkeypatch):
    _base(monkeypatch, te="telugu_native", hi="hindi_native")
    assert S.tts_speaker_for("te", {}) == "telugu_native"
    assert S.tts_speaker_for("hi", {}) == "hindi_native"
    assert S.tts_speaker_for("en", {}) == "shubh"   # no en override → default
    assert S.tts_speaker_for("ta", {}) == "shubh"   # unmapped language → default


def test_bcp47_code_normalizes_to_short(monkeypatch):
    _base(monkeypatch, te="telugu_native")
    assert S.tts_speaker_for("te-IN", {}) == "telugu_native"


def test_tenant_global_override_beats_per_language(monkeypatch):
    _base(monkeypatch, te="telugu_native")
    ps = {"sarvam_tts_speaker": "tenant_voice"}
    assert S.tts_speaker_for("te", ps) == "tenant_voice"
    assert S.tts_speaker_for("hi", ps) == "tenant_voice"
    # legacy key also honoured
    assert S.tts_speaker_for("te", {"tts_voice": "legacy_voice"}) == "legacy_voice"


def test_tenant_per_language_override_beats_env(monkeypatch):
    _base(monkeypatch, te="telugu_native")
    ps = {"sarvam_tts_speaker_te": "tenant_telugu"}
    assert S.tts_speaker_for("te", ps) == "tenant_telugu"
    assert S.tts_speaker_for("hi", ps) == "shubh"   # hi unaffected


def _base_pace(monkeypatch, *, te=1.2, hi=1.0, en=1.0):
    monkeypatch.setattr(settings, "SARVAM_TTS_PACE_TE", te)
    monkeypatch.setattr(settings, "SARVAM_TTS_PACE_HI", hi)
    monkeypatch.setattr(settings, "SARVAM_TTS_PACE_EN", en)


def test_pace_for_bumps_telugu_including_when_unset(monkeypatch):
    _base_pace(monkeypatch, te=1.2)
    # No explicit pace → Telugu still gets the bump (baseline 1.0 * 1.2).
    assert S.pace_for("te", None) == 1.2
    assert S.pace_for("te-IN", 1.0) == 1.2
    # A tone pace is multiplied, then clamped to bulbul's 0.3–3.0 range.
    assert round(S.pace_for("te", 0.97), 3) == 1.164


def test_pace_for_leaves_other_languages_unchanged(monkeypatch):
    _base_pace(monkeypatch, te=1.2, hi=1.0, en=1.0)
    assert S.pace_for("en", 1.0) == 1.0
    assert S.pace_for("en", None) is None   # factor 1.0 + no pace → no change
    assert S.pace_for("hi", None) is None
