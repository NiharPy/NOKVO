"""Outbound voice humanization: barge-in immunity + pace/EOU tuning.

Covers the pure, unit-testable pieces of the outbound humanization change:
  * ``_is_backchannel_utterance`` — the multilingual "uh-huh"/cough guard that
    stops short backchannels from cancelling the agent (vad_blob backstop).
  * ``_scaled_pace`` — the outbound TTS pace multiplier, including the load-
    bearing "factor 1.0 leaves inbound byte-identical (None stays None)" rule.
  * Config: the new knobs exist and the outbound EOU tiers DEFAULT to the global
    values (so enabling outbound scoping is a no-op until an operator tunes them).

The streaming speech_start debounce + cancel wiring is integration-level (deeply
nested in run_session) and is verified by the manual-call checklist in the plan,
not here.
"""
from __future__ import annotations

import app.services.nokvo_one_voice_stream_service as ss
from app.core.config import settings


# ── Backchannel / cough immunity ───────────────────────────────────────────

def test_backchannel_recognises_multilingual_acks():
    bc = ss._is_backchannel_utterance
    for w in ["uh-huh", "uhhuh", "yeah", "yep", "ok", "okay", "right", "got it",
              "i see", "hmm", "mm-hmm",
              "haan", "हाँ", "ji", "जी", "achha", "अच्छा", "theek hai",
              "avunu", "అవును", "sare", "సరే",
              "ஆமா", "சரி"]:
        assert bc(w), f"expected backchannel: {w!r}"


def test_backchannel_rejects_real_utterances():
    bc = ss._is_backchannel_utterance
    # Real answers / questions are NOT backchannels — they must still barge in.
    assert bc("yes please continue") is False   # >2 words
    assert bc("what is your budget") is False
    assert bc("Kollur") is False                # 1 word but not an ack
    assert bc("three BHK") is False


def test_backchannel_empty_is_suppressed():
    # A cough that transcribes to nothing/punctuation should NOT cancel the agent.
    assert ss._is_backchannel_utterance("") is True
    assert ss._is_backchannel_utterance("   ") is True
    assert ss._is_backchannel_utterance("...") is True


def test_backchannel_trims_trailing_punctuation_and_case():
    assert ss._is_backchannel_utterance("Yeah.") is True
    assert ss._is_backchannel_utterance("OK?") is True
    assert ss._is_backchannel_utterance("Got it!") is True


# ── Pace scaling ────────────────────────────────────────────────────────────

def test_scaled_pace_factor_one_is_identity():
    # Inbound (factor 1.0) must be byte-identical: None stays None, value stays.
    assert ss._scaled_pace(None, 1.0) is None
    assert ss._scaled_pace(0.9, 1.0) == 0.9
    assert ss._scaled_pace(1.08, 1.0) == 1.08


def test_scaled_pace_applies_to_none_baseline():
    # First sentence has no per-tone pace (None) — the factor must still apply,
    # using the neutral 1.0 baseline, so EVERY outbound sentence slows down.
    assert abs(ss._scaled_pace(None, 0.95) - 0.95) < 1e-9
    assert abs(ss._scaled_pace(1.0, 0.95) - 0.95) < 1e-9
    assert abs(ss._scaled_pace(0.9, 0.95) - 0.855) < 1e-9


def test_scaled_pace_clamps_to_sarvam_range():
    assert ss._scaled_pace(0.2, 0.95) == 0.3   # floor
    assert ss._scaled_pace(5.0, 0.95) == 3.0   # ceiling


# ── Config knobs ────────────────────────────────────────────────────────────

def test_humanization_settings_exist_with_safe_defaults():
    assert 0.3 <= settings.VOICE_OUTBOUND_PACE_FACTOR <= 1.0
    assert settings.VOICE_BARGE_IN_MIN_MS >= 100


def test_outbound_eou_defaults_match_global():
    # Outbound EOU tiers default to the global values, so scoping EOU to outbound
    # is a NO-OP until an operator deliberately tunes the *_OUTBOUND knobs.
    assert settings.VOICE_EOU_COMPLETE_MS_OUTBOUND == settings.VOICE_EOU_COMPLETE_MS
    assert settings.VOICE_EOU_NEUTRAL_MS_OUTBOUND == settings.VOICE_EOU_NEUTRAL_MS
    assert settings.VOICE_EOU_DEBOUNCE_MS_OUTBOUND == settings.VOICE_EOU_DEBOUNCE_MS
    assert (
        settings.VOICE_EOU_CONTINUATION_BONUS_MS_OUTBOUND
        == settings.VOICE_EOU_CONTINUATION_BONUS_MS
    )


# ── the humanization layer is actually ON ────────────────────────────────────
# All of this was built, tested and then shipped with every knob at its off/zero
# default — one of them still carried "# rollout value ~800" beside a live 0. The
# audible result was that every APEX call was the byte-identical waveform after a
# near-constant response gap: robotic to a human, and a trivial fingerprint for a
# carrier-side spam classifier.


def test_humanization_layer_is_enabled():
    assert settings.APEX_TURN_GAP_TARGET_MS > 0, "response gap shaping is off"
    assert settings.APEX_TTS_VARIANTS > 1, "every call would share one waveform"
    assert settings.APEX_OPENER_VARIANTS > 1, "every call would open identically"
    assert settings.APEX_ACK_ENABLED is True


def test_acks_are_english_only_until_the_indic_pools_are_reviewed():
    """The hi/te pools are marked in-code as drafts pending native-speaker
    review. Enabling the feature must not ship them."""
    from app.services.apex_micro_acks import _enabled_ack_languages

    langs = _enabled_ack_languages()
    assert "en" in langs
    assert "hi" not in langs and "te" not in langs


def test_choose_ack_respects_the_language_gate(monkeypatch):
    from app.services import apex_micro_acks as ma

    monkeypatch.setattr(settings, "APEX_ACK_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "APEX_ACK_PROBABILITY", 1.0, raising=False)
    monkeypatch.setattr(settings, "APEX_ACK_LANGUAGES", "en", raising=False)

    def ack(lang):
        return ma.choose_ack(call_id="c1", question_idx=2, language=lang, delivered_count=1)

    assert ack("en")            # reviewed language speaks
    assert ack("hi") is None    # unreviewed stays silent
    assert ack("te") is None
    # Silence, never a fallback: an English "Got it." between Telugu questions
    # would be worse than no ack at all.


def test_widening_the_gate_re_enables_a_language(monkeypatch):
    from app.services import apex_micro_acks as ma

    monkeypatch.setattr(settings, "APEX_ACK_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "APEX_ACK_PROBABILITY", 1.0, raising=False)
    monkeypatch.setattr(settings, "APEX_ACK_LANGUAGES", "en,hi,te", raising=False)
    assert ma.choose_ack(call_id="c1", question_idx=2, language="te", delivered_count=1)
