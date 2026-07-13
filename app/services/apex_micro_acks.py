"""Deterministic micro-acknowledgments + per-call TTS rendition selection.

Humanization for the APEX deterministic (verbatim) questionnaire path. Two
pure, seeded helpers live here so both the voice stream orchestrator and the
TTS prewarmer import them without cycles:

* ``choose_ack`` — a short, warm, native-script acknowledgment ("సరే.",
  "जी.", "Got it.") spoken before the next verbatim question on ~45% of
  clean advances. The LLM path BANS stock acks because gpt-5-mini repeated
  the same one every turn; this layer is deterministic — seeded per
  call+question, never repeating the previous ack, skipped on the first
  question — so it restores the human answer→ack→question cadence without
  restoring the repetition.

* ``tts_variant_for_call`` — picks which cached rendition (1..N) of a
  scripted line a call speaks. Stable within a call (one "take" per call,
  the way a single human sounds) and spread across calls. crc32, NEVER the
  builtin ``hash()`` (process-salted → workers would disagree).

Both consult ``settings`` at call time so env flips need no restart-order
care, and both are total functions: bad input degrades to "no ack" /
"variant 1", never raises.
"""
from __future__ import annotations

import random
import zlib

from app.core.config import settings

# Short warm acks per language, matching the language_style register (native
# script for hi/te — Sarvam mispronounces romanised Indic). Deliberately ONLY
# en/hi/te: the questionnaire is pre-translated into exactly these, and an
# English "Got it." on a Tamil call would be worse than silence.
ACK_POOLS: dict[str, tuple[str, ...]] = {
    "en": ("Okay.", "Got it.", "Alright.", "Perfect.", "Thanks."),
    "hi": ("जी.", "ठीक है.", "अच्छा.", "समझ गया.", "बिलकुल."),
    "te": ("సరే.", "అలాగే.", "మంచిది.", "అర్థమైంది."),
}


def _lang_key(language: str | None) -> str:
    """``en-IN`` → ``en``; unknown/empty → `` `` (no pool)."""
    return str(language or "").split("-")[0].strip().lower()


def choose_ack(
    *,
    call_id: str,
    question_idx: int,
    language: str | None,
    last_ack: str | None = None,
    delivered_count: int = 0,
) -> str | None:
    """The ack to speak before question ``question_idx``, or ``None`` to skip.

    ``None`` when: the feature flag is off; the language has no pool; nothing
    has been delivered yet (``delivered_count == 0`` — never ack the reply to
    the opener/consent, and Q1 therefore never gets one); or the seeded
    probability gate (``APEX_ACK_PROBABILITY``) doesn't fire for this
    call+question. The RNG seed is ``"{call_id}:{question_idx}"`` so the
    decision is reproducible per turn in debugging yet varied across calls,
    and independent of the rendition-variant seed (ack pattern and voice take
    don't correlate). ``last_ack`` is excluded so the same ack never plays
    twice running.
    """
    if not settings.APEX_ACK_ENABLED:
        return None
    if delivered_count <= 0:
        return None
    pool = ACK_POOLS.get(_lang_key(language))
    if not pool:
        return None
    rng = random.Random(f"{call_id}:{question_idx}")
    try:
        probability = float(settings.APEX_ACK_PROBABILITY)
    except (TypeError, ValueError):
        probability = 0.0
    if rng.random() >= probability:
        return None
    candidates = [a for a in pool if a != (last_ack or "")]
    if not candidates:
        return None
    return rng.choice(candidates)


def tts_variant_for_call(call_id: str | None, n: int | None = None) -> int:
    """Which cached rendition (1..N) of every scripted line this call speaks.

    ``N`` defaults to ``settings.APEX_TTS_VARIANTS``. Returns 1 (today's sole
    rendition — adds no cache-key salt) when N <= 1 or there's no call id.
    """
    count = int(n if n is not None else (settings.APEX_TTS_VARIANTS or 1))
    if count <= 1 or not call_id:
        return 1
    return (zlib.crc32(str(call_id).encode("utf-8")) % count) + 1
