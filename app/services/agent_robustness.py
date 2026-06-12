"""Unified robustness layer for the Nokvo One voice agent.

The pipeline already has scattered support for four classes of caller-side
failure — bad audio, mixed-language speech, mid-answer interruptions, and
vague / underspecified requests. The handling is spread across the STT
client, the stream service, the pipeline router, and the intent router.

This module is the systematic version of that support. Each primitive is
a single object the call-handling code interacts with rather than
re-implementing the same heuristic inline:

* :class:`AudioQualityProbe` — fast PCM-only signal-quality score used to
  short-circuit a turn before paying STT/LLM/TTS latency on an
  unintelligible utterance.
* :class:`LanguageState` — per-call language tracker that records the
  last few detected languages, flags code-switch, and decides which
  retrieval and reply languages to use.
* :class:`TurnArbiter` — explicit turn lifecycle (idle/thinking/
  speaking/done) with deterministic barge-in vs check-in arbitration.
  Owns the LLM-stream task + TTS pump so cancellation is atomic.
* :class:`ClarificationState` — escalation FSM for vague turns. After
  two unhelpful turns in a row it offers caller-facing options; after a
  third it escalates to support.

The primitives are intentionally framework-agnostic — they don't import
websocket / Redis / SQLAlchemy. The voice-stream service and the pipeline
wire them in at the appropriate seams.
"""

from __future__ import annotations

import asyncio
import re
import struct
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Awaitable, Callable, Iterable


# ── Audio quality scoring ───────────────────────────────────────────────────

# Indian-script ranges, used by the language-state code-switch heuristic
# and the audio probe to know when "no transcript" + low energy is just
# silence vs an STT miss on real speech.
_INDIAN_SCRIPT_RE = re.compile(
    "[ऀ-ॿ"   # Devanagari (Hindi, Marathi)
    "ঀ-৿"    # Bengali
    "਀-੿"    # Gurmukhi (Punjabi)
    "઀-૿"    # Gujarati
    "଀-୿"    # Odia
    "஀-௿"    # Tamil
    "ఀ-౿"    # Telugu
    "ಀ-೿"    # Kannada
    "ഀ-ൿ"    # Malayalam
    "؀-ۿ"    # Arabic (Urdu)
    "]"
)
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")


def _has_indian_script(text: str) -> bool:
    return bool(_INDIAN_SCRIPT_RE.search(text or ""))


def _has_latin_letters(text: str) -> bool:
    return bool(_LATIN_LETTER_RE.search(text or ""))


# Quality verdicts. UNUSABLE means the caller should be asked to repeat;
# POOR means the turn should run but downstream code may want to widen
# retrieval / increase clarification weight; OK is the happy path.
QUALITY_OK = "ok"
QUALITY_POOR = "poor"
QUALITY_UNUSABLE = "unusable"


@dataclass(frozen=True)
class AudioQuality:
    """Snapshot of a single utterance's signal quality. Computed from raw
    PCM without a third-party DSP dep — just stdlib ``struct``.

    Fields are all in well-defined ranges so callers can compare across
    utterances or tune thresholds without re-deriving them:

    * ``rms`` — root-mean-square amplitude (0.0–1.0 of the int16 range).
    * ``peak`` — peak amplitude (0.0–1.0 of the int16 range). High peak
      with low RMS suggests clipping or impulsive noise.
    * ``clip_ratio`` — fraction of samples within 0.5 dB of full scale.
      Sustained clipping warps the spectrum and STT typically returns
      garbage for those frames.
    * ``silence_ratio`` — fraction of frames whose energy is below a
      ``-50 dBFS`` floor. A near-silent utterance after VAD-trigger
      usually means the caller was just breathing or background noise
      tripped the VAD.
    * ``duration_ms`` — utterance length, used to disambiguate "0.2 s of
      audio" (almost certainly noise) from "5 s of audio with all-silent
      frames" (real signal but the caller spoke very quietly).
    * ``verdict`` — derived label, one of :data:`QUALITY_OK`,
      :data:`QUALITY_POOR`, :data:`QUALITY_UNUSABLE`. Driven from the raw
      metrics by :meth:`AudioQualityProbe.score`.
    * ``reason`` — short human-readable summary attached to the verdict.
    """

    rms: float
    peak: float
    clip_ratio: float
    silence_ratio: float
    duration_ms: int
    verdict: str
    reason: str

    @property
    def usable(self) -> bool:
        return self.verdict != QUALITY_UNUSABLE


class AudioQualityProbe:
    """Tiny DSP probe that scores PCM16-LE mono audio.

    The implementation is deliberately minimal — Python-level
    ``struct.unpack`` over a few thousand samples completes in well under
    a millisecond even on cold CPU. It does not pull in numpy/scipy; the
    voice pipeline runs in a hot async loop and we don't want to add a
    blocking dep here.

    Thresholds were picked from observed call-quality histograms:
    typical clean speech sits at ``rms ~0.04-0.10``, mobile-network
    speech around ``rms ~0.02``, anything below ``0.005`` is usually
    just line noise. The numbers are exposed as class attributes so a
    deployment that sees different mics / encodings can override them
    without rewriting the probe.
    """

    SAMPLE_FULL_SCALE = 32767.0
    CLIP_THRESHOLD = 31_000      # int16 samples within ~0.6 dB of FS
    SILENT_FRAME_RMS = 0.00316   # ~-50 dBFS
    FRAME_MS = 20                # window length for silence ratio

    UNUSABLE_MIN_DURATION_MS = 250
    UNUSABLE_MAX_SILENCE_RATIO = 0.92
    UNUSABLE_MIN_RMS = 0.0035
    UNUSABLE_MAX_CLIP_RATIO = 0.25

    POOR_MIN_RMS = 0.012
    POOR_MAX_CLIP_RATIO = 0.05
    POOR_MAX_SILENCE_RATIO = 0.75

    @classmethod
    def score(cls, pcm: bytes, *, sample_rate: int) -> AudioQuality:
        """Score a buffer of signed 16-bit LE mono PCM."""
        if not pcm:
            return AudioQuality(0.0, 0.0, 0.0, 1.0, 0, QUALITY_UNUSABLE, "empty buffer")
        sample_count = len(pcm) // 2
        if sample_count == 0:
            return AudioQuality(0.0, 0.0, 0.0, 1.0, 0, QUALITY_UNUSABLE, "no samples")
        duration_ms = int((sample_count / sample_rate) * 1000)
        # Unpack in one go — much faster than per-sample iteration.
        samples = struct.unpack(f"<{sample_count}h", pcm[: sample_count * 2])

        total_sq = 0
        peak = 0
        clip_count = 0
        for s in samples:
            sq = s * s
            total_sq += sq
            abs_s = -s if s < 0 else s
            if abs_s > peak:
                peak = abs_s
            if abs_s >= cls.CLIP_THRESHOLD:
                clip_count += 1
        rms_raw = (total_sq / sample_count) ** 0.5
        rms = rms_raw / cls.SAMPLE_FULL_SCALE
        peak_norm = peak / cls.SAMPLE_FULL_SCALE
        clip_ratio = clip_count / sample_count

        # Silence ratio: per-frame RMS. We could compute it during the
        # main pass but the second pass keeps the code shorter and stays
        # well below 1 ms even on a 10-second utterance.
        frame_size = max(1, int(sample_rate * cls.FRAME_MS / 1000))
        frame_count = max(1, sample_count // frame_size)
        silent_frames = 0
        for f_idx in range(frame_count):
            start = f_idx * frame_size
            chunk = samples[start : start + frame_size]
            if not chunk:
                break
            frame_sq = sum(s * s for s in chunk) / len(chunk)
            frame_rms = (frame_sq ** 0.5) / cls.SAMPLE_FULL_SCALE
            if frame_rms < cls.SILENT_FRAME_RMS:
                silent_frames += 1
        silence_ratio = silent_frames / frame_count

        verdict, reason = cls._verdict(
            rms=rms,
            clip_ratio=clip_ratio,
            silence_ratio=silence_ratio,
            duration_ms=duration_ms,
        )
        return AudioQuality(
            rms=rms,
            peak=peak_norm,
            clip_ratio=clip_ratio,
            silence_ratio=silence_ratio,
            duration_ms=duration_ms,
            verdict=verdict,
            reason=reason,
        )

    @classmethod
    def _verdict(
        cls,
        *,
        rms: float,
        clip_ratio: float,
        silence_ratio: float,
        duration_ms: int,
    ) -> tuple[str, str]:
        if duration_ms < cls.UNUSABLE_MIN_DURATION_MS:
            return QUALITY_UNUSABLE, f"too short ({duration_ms}ms)"
        if silence_ratio >= cls.UNUSABLE_MAX_SILENCE_RATIO:
            return QUALITY_UNUSABLE, "mostly silence"
        if rms < cls.UNUSABLE_MIN_RMS:
            return QUALITY_UNUSABLE, "near-silent signal"
        if clip_ratio >= cls.UNUSABLE_MAX_CLIP_RATIO:
            return QUALITY_UNUSABLE, "heavy clipping"
        # Below this is "salvageable but caller should be encouraged to
        # speak more clearly".
        if (
            rms < cls.POOR_MIN_RMS
            or clip_ratio >= cls.POOR_MAX_CLIP_RATIO
            or silence_ratio >= cls.POOR_MAX_SILENCE_RATIO
        ):
            return QUALITY_POOR, "low SNR / clipped"
        return QUALITY_OK, "clean"


class AudioEnhancer:
    """Stateful, per-call inbound audio conditioner for STT.

    Forwarded telephony audio reaches us quiet and variable-level (caller →
    carrier → forward → Plivo → us), and STT garbles quiet/uneven speech. This
    conditions each PCM chunk before STT:

    * **~100 Hz high-pass** — a one-pole biquad with persistent state across
      chunks. Carrier networks nominally high-pass ~300 Hz, but forwarded /
      VoLTE legs leak low-frequency rumble and handling noise that wastes AGC
      headroom; killing it below the voice band costs nothing audible.
    * **DC-offset removal** — subtract the chunk mean. Belt-and-braces after
      the high-pass (a chunk-mean step is cheap and keeps the old invariant).
    * **Smoothed AGC** — track a running RMS and apply a gain that moves the
      signal toward a target level (``target_dbfs``). The gain is attack/release
      smoothed so it never *pumps* between chunks. Near-silent chunks (below
      the probe's silence floor) are left alone so we don't amplify pure line
      noise, and a slow noise-floor tracker shrinks the allowed gain on
      hiss-only lines (poor SNR → boosting just makes louder hiss).
    * **Soft-knee limiter** — linear below the knee, tanh-shaped compression
      above it, hard ceiling at full scale. A loud burst lands smoothly
      instead of square-clipping (which STT reads as distortion).

    numpy is imported lazily inside :meth:`process` so this module stays
    dependency-free at import time — it runs in the Plivo bridge where numpy is
    already loaded. One instance per call; not thread-safe by design.
    """

    FULL_SCALE = AudioQualityProbe.SAMPLE_FULL_SCALE  # 32767.0
    SILENCE_RMS = AudioQualityProbe.SILENT_FRAME_RMS  # normalized ~-50 dBFS
    MAX_GAIN = 8.0   # never boost more than +18 dB (avoid blowing up noise)
    MIN_GAIN = 0.5   # let a hot line come down a little, but not silence it
    HIGHPASS_HZ = 100.0       # below the voice band; kills rumble/handling noise
    LIMITER_KNEE = 0.5        # fraction of full scale where soft compression starts
    # Noise-floor tracking: floor*gain must stay under SILENCE_RMS * this
    # factor — i.e. amplified line noise may not rise far above the silence
    # threshold the probe / EOU logic treats as "quiet".
    NOISE_HEADROOM = 4.0

    def __init__(
        self,
        *,
        target_dbfs: float = -20.0,
        attack: float = 0.5,
        release: float = 0.08,
        sample_rate: int = 16000,
    ) -> None:
        # Target RMS as a fraction of full scale (0..1).
        self._target_rms = float(10.0 ** (target_dbfs / 20.0))
        self._attack = float(attack)    # gain moving UP (toward louder) — quick
        self._release = float(release)  # gain moving DOWN — slow, avoids pumping
        self._gain = 1.0
        self._last_rms = 0.0
        self._chunks = 0
        # High-pass one-pole coefficient + persistent filter state. A first-
        # order HPF at ~100 Hz: y[n] = a*(y[n-1] + x[n] - x[n-1]).
        import math
        rc = 1.0 / (2.0 * math.pi * self.HIGHPASS_HZ)
        dt = 1.0 / max(8000, int(sample_rate))
        self._hp_a = rc / (rc + dt)
        self._hp_prev_x = 0.0
        self._hp_prev_y = 0.0
        # Noise-floor estimate (normalized RMS), learned ONLY from near-silent
        # chunks — i.e. the line's resting level between words. 0.0 until a
        # quiet gap has been observed; speech/tone chunks never update it.
        self._noise_floor = 0.0

    @property
    def last_rms(self) -> float:
        """Normalised RMS (0..1) of the most recent processed chunk."""
        return self._last_rms

    @property
    def gain(self) -> float:
        return self._gain

    def process(self, pcm: bytes) -> bytes:
        """Condition one buffer of signed 16-bit LE mono PCM. Returns the
        enhanced bytes (same length). Best-effort: any failure returns the
        input unchanged so a DSP glitch never drops caller audio."""
        if not pcm:
            return pcm
        try:
            import numpy as np

            x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            if x.size == 0:
                return pcm
            # ~100 Hz high-pass (state carried across chunks) — rumble eats
            # AGC headroom and skews the RMS tracker. First-order recursive
            # HPF y[n] = a*(y[n-1] + x[n] - x[n-1]); the scalar loop costs
            # well under 0.5 ms per typical 20 ms chunk (no scipy in repo).
            diffs = np.empty_like(x)
            diffs[0] = x[0] - self._hp_prev_x
            diffs[1:] = x[1:] - x[:-1]
            yacc = self._hp_prev_y
            out = np.empty(x.size, dtype=np.float64)
            ad = float(self._hp_a)
            d64 = diffs.astype(np.float64)
            for i in range(x.size):
                yacc = ad * (yacc + d64[i])
                out[i] = yacc
            self._hp_prev_x = float(x[-1])
            self._hp_prev_y = float(yacc)
            x = out.astype(np.float32)
            # DC-offset removal (belt-and-braces after the HPF).
            x = x - float(x.mean())
            rms = float(np.sqrt(np.mean((x / self.FULL_SCALE) ** 2))) if x.size else 0.0
            self._last_rms = rms
            self._chunks += 1
            # Noise-floor tracker: learn the line's resting level from
            # near-silent chunks only (the gaps between words) — a steady
            # speech tone must never be mistaken for noise.
            if rms <= self.SILENCE_RMS * 3.0:
                self._noise_floor = (
                    rms if self._noise_floor == 0.0 else 0.8 * self._noise_floor + 0.2 * rms
                )
            # Only chase the target when there's real signal — never amplify
            # pure line noise / silence.
            if rms > self.SILENCE_RMS:
                desired = self._target_rms / max(rms, 1e-6)
                desired = min(self.MAX_GAIN, max(self.MIN_GAIN, desired))
                # Adaptive cap: amplified line noise must stay near the
                # silence threshold. Only binds when the measured between-
                # words floor is itself above the silence threshold (a hissy
                # line) — boosting that just serves STT louder hiss.
                if self._noise_floor > self.SILENCE_RMS:
                    noise_cap = (self.SILENCE_RMS * self.NOISE_HEADROOM) / self._noise_floor
                    desired = min(desired, max(1.0, noise_cap))
                coef = self._attack if desired > self._gain else self._release
                self._gain = (1.0 - coef) * self._gain + coef * desired
            y = x * self._gain
            # Soft-knee limiter: linear below the knee, tanh compression
            # above, hard ceiling at full scale. No square-clip distortion.
            knee = self.LIMITER_KNEE * self.FULL_SCALE
            span = self.FULL_SCALE - knee
            mag = np.abs(y)
            over = mag > knee
            if bool(over.any()):
                compressed = knee + span * np.tanh((mag[over] - knee) / span)
                y = y.copy()
                y[over] = np.sign(y[over]) * compressed
            y = np.clip(y, -self.FULL_SCALE, self.FULL_SCALE)
            return y.astype(np.int16).tobytes()
        except Exception:
            return pcm


def trim_leading_silence(
    pcm: bytes,
    *,
    sample_rate: int = 16000,
    preroll_ms: int = 100,
    floor: float | None = None,
) -> bytes:
    """Drop leading sub-floor audio from a PCM16 mono buffer, keeping
    ``preroll_ms`` of context before the first voiced frame.

    Leading dead air costs STT accuracy on short utterances (the model
    anchors on noise) and wastes upload bytes. Pure helper, best-effort:
    any failure returns the input unchanged. Never trims everything — if no
    voiced frame is found the buffer is returned as-is (the quality probe
    owns the "all silence" verdict).
    """
    if not pcm or len(pcm) < 4:
        return pcm
    try:
        import numpy as np

        silence_floor = float(floor if floor is not None else AudioQualityProbe.SILENT_FRAME_RMS)
        usable = len(pcm) - (len(pcm) % 2)
        x = np.frombuffer(pcm[:usable], dtype=np.int16).astype(np.float32)
        frame = max(1, int(sample_rate * 0.02))  # 20 ms frames
        n_frames = x.size // frame
        if n_frames < 2:
            return pcm
        frames = x[: n_frames * frame].reshape(n_frames, frame)
        rms = np.sqrt(np.mean((frames / AudioQualityProbe.SAMPLE_FULL_SCALE) ** 2, axis=1))
        voiced = np.nonzero(rms > silence_floor)[0]
        if voiced.size == 0:
            return pcm
        first = int(voiced[0])
        preroll_frames = max(0, int(round(preroll_ms / 20.0)))
        start_frame = max(0, first - preroll_frames)
        if start_frame == 0:
            return pcm
        return pcm[start_frame * frame * 2:]
    except Exception:
        return pcm


# Pre-canned multilingual "I missed that, could you repeat" prompts. Kept
# inside the robustness module so the recovery layer doesn't have to reach
# back into the pipeline for refusal strings — the stream service can
# dispatch a recovery reply directly from a probe verdict.
_REPEAT_PROMPTS = {
    "en": "I couldn't catch that clearly — could you say it again, a bit louder?",
    "hi": "मुझे ठीक से सुनाई नहीं दिया — क्या आप थोड़ा ज़ोर से दोबारा कह सकते हैं?",
    "ta": "சரியாக கேட்கவில்லை — கொஞ்சம் சத்தமாக மீண்டும் சொல்ல முடியுமா?",
    "te": "సరిగ్గా వినపడలేదు — కొంచెం గట్టిగా మళ్ళీ చెప్పగలరా?",
    "bn": "ঠিক মতো শুনতে পেলাম না — একটু জোরে আবার বলবেন?",
    "kn": "ಸ್ಪಷ್ಟವಾಗಿ ಕೇಳಿಸಲಿಲ್ಲ — ಸ್ವಲ್ಪ ಜೋರಾಗಿ ಮತ್ತೆ ಹೇಳಿ.",
    "ml": "വ്യക്തമായി കേട്ടില്ല — ഒന്നുകൂടി പറയാമോ?",
    "mr": "नीट ऐकू आलं नाही — पुन्हा सांगाल का?",
    "gu": "બરાબર સંભળાયું નહીં — ફરી કહેશો?",
    "pa": "ਠੀਕ ਤਰ੍ਹਾਂ ਸੁਣਾਈ ਨਹੀਂ ਦਿੱਤਾ — ਕਿਰਪਾ ਕਰਕੇ ਦੁਬਾਰਾ ਬੋਲੋ।",
}


def repeat_prompt(language: str | None) -> str:
    code = (language or "en").strip().lower()[:2]
    return _REPEAT_PROMPTS.get(code, _REPEAT_PROMPTS["en"])


# ── Mixed-language tracking ─────────────────────────────────────────────────


@dataclass
class LanguageState:
    """Per-call language history + code-switch detection.

    The pipeline already does the right thing for *single-language* calls
    (sticky language lock after the first detected utterance). It is the
    *code-switching* calls — Indian English speakers who slip Telugu /
    Hindi loanwords into otherwise-English sentences — where the existing
    heuristic falls short:

    * sticky lock prevents the agent from following the speaker into a
      genuine language change later in the call.
    * single retrieval query (native OR translated) misses chunks that
      sit on the other side of the language barrier.

    This object tracks the last few detected languages, flags when the
    call is actively code-switching, and recommends a retrieval strategy
    accordingly.
    """

    locked_language: str | None = None
    history: list[str] = field(default_factory=list)
    history_max: int = 5
    # When True, the next retrieval call should use BOTH the native and
    # the translated form of the utterance and union the results.
    code_switching: bool = False
    # Per-turn record of the script makeup of the transcript.
    last_script_mix: str = ""

    def observe(self, language: str | None, transcript: str = "") -> None:
        """Record one turn's detected language + script makeup."""
        code = (language or "").strip().lower()[:2]
        if code:
            self.history.append(code)
            if len(self.history) > self.history_max:
                self.history = self.history[-self.history_max :]
        # Script-mix detection: Indian-script + Latin letters in the same
        # transcript almost always indicates code-switching (Indian
        # English speakers transliterate proper nouns / numbers while
        # speaking the rest in a native script, or vice versa).
        if transcript:
            has_indian = _has_indian_script(transcript)
            has_latin = _has_latin_letters(transcript)
            if has_indian and has_latin:
                self.last_script_mix = "mixed"
            elif has_indian:
                self.last_script_mix = "native"
            elif has_latin:
                self.last_script_mix = "latin"
            else:
                self.last_script_mix = ""

        # Code-switch verdict: two distinct languages in the recent
        # history window, OR the latest transcript is itself
        # script-mixed.
        unique = {h for h in self.history if h}
        self.code_switching = len(unique) >= 2 or self.last_script_mix == "mixed"

    def lock(self, language: str | None) -> str | None:
        code = (language or "").strip().lower()[:2]
        if code:
            self.locked_language = code
        return self.locked_language

    def dominant_language(self, fallback: str | None = None) -> str:
        """Pick the reply language. Locked language wins; otherwise use
        the most-frequent of the recent history; finally fall back."""
        if self.locked_language:
            return self.locked_language
        if self.history:
            counts: dict[str, int] = {}
            for h in self.history:
                counts[h] = counts.get(h, 0) + 1
            return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        return (fallback or "en").strip().lower()[:2] or "en"

    def needs_dual_retrieval(self) -> bool:
        """``True`` when the retrieval layer should run BOTH the native
        and the English-translated queries and merge the results."""
        return self.code_switching


# ── Clarification FSM ───────────────────────────────────────────────────────


CLARIFY_RESET = "reset"
CLARIFY_NUDGE = "nudge"
CLARIFY_OFFER_OPTIONS = "offer_options"
CLARIFY_ESCALATE = "escalate"


@dataclass
class ClarificationState:
    """Tracks consecutive low-information turns so the agent can escalate
    gracefully instead of looping the same "sorry, could you tell me
    more?" reply forever.

    The state is intentionally tiny so it can serialise into the Redis
    session blob without bloating the per-turn payload. It is read
    before answering and written after.

    Lifecycle:

    * caller utterance is clear → :meth:`reset` (counter back to 0).
    * caller utterance is vague (no intent, no retrieval, no FSM slot
      progress) → :meth:`bump` (counter += 1) and use :meth:`action`
      to pick the next caller-facing response: a soft nudge, then an
      explicit options menu, then an escalation handoff.
    """

    consecutive_vague_turns: int = 0
    last_unanswered_query: str | None = None

    def bump(self, query: str | None = None) -> None:
        self.consecutive_vague_turns = min(self.consecutive_vague_turns + 1, 6)
        if query:
            self.last_unanswered_query = query[:200]

    def reset(self) -> None:
        self.consecutive_vague_turns = 0
        self.last_unanswered_query = None

    def action(self) -> str:
        """Pick the next caller-facing recovery action based on how many
        vague turns have happened in a row."""
        n = self.consecutive_vague_turns
        if n <= 0:
            return CLARIFY_RESET
        if n == 1:
            return CLARIFY_NUDGE
        if n == 2:
            return CLARIFY_OFFER_OPTIONS
        return CLARIFY_ESCALATE

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> "ClarificationState":
        data = data or {}
        return ClarificationState(
            consecutive_vague_turns=int(data.get("consecutive_vague_turns") or 0),
            last_unanswered_query=data.get("last_unanswered_query"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "consecutive_vague_turns": self.consecutive_vague_turns,
            "last_unanswered_query": self.last_unanswered_query,
        }


_CLARIFY_NUDGE = {
    "en": "Sorry, I missed that — could you tell me what you need help with?",
    "hi": "माफ़ कीजिए, समझ नहीं आया — कृपया बताइए आपको किस चीज़ में मदद चाहिए?",
    "ta": "மன்னிக்கவும், எனக்கு புரியவில்லை — என்ன உதவி வேண்டும் என்று சொல்ல முடியுமா?",
    "te": "క్షమించండి, అర్థం కాలేదు — ఏ విషయంలో సహాయం కావాలో చెప్పగలరా?",
    "bn": "মাফ করবেন, বুঝতে পারিনি — কী ব্যাপারে সাহায্য চান বলবেন?",
}
# Vertical-neutral options. The previous wording ("bookings, cancellations,
# refunds") read like a generic support bot and is wrong for real-estate /
# clinic / sales agents. "details, pricing, or book" is universal and on-brand.
_CLARIFY_OPTIONS = {
    "en": "Happy to help — would you like details, pricing, or to book something?",
    "hi": "मैं मदद के लिए हूँ — क्या आपको जानकारी, कीमत, या कुछ बुक करना है?",
    "ta": "உதவ தயார் — உங்களுக்கு விவரங்கள், விலை, அல்லது ஏதாவது பதிவு செய்ய வேண்டுமா?",
    "te": "సహాయం చేయడానికి సిద్ధం — మీకు వివరాలు, ధర, లేదా ఏదైనా బుక్ చేయాలా?",
    "bn": "সাহায্য করতে প্রস্তুত — আপনি কি তথ্য, দাম, নাকি কিছু বুক করতে চান?",
}
_CLARIFY_ESCALATE = {
    "en": "I'm having trouble understanding — let me connect you with a person who can help.",
    "hi": "मुझे समझने में दिक्कत हो रही है — मैं आपको एक एजेंट से जोड़ देता हूँ।",
    "ta": "எனக்கு புரிந்துகொள்ள சிரமமாக உள்ளது — நான் உங்களை ஒரு பிரதிநிதியுடன் இணைக்கிறேன்.",
    "te": "నాకు అర్థం కాడంలో ఇబ్బంది వస్తోంది — మిమ్మల్ని ఏజెంట్‌తో కలుపుతున్నాను.",
    "bn": "বুঝতে অসুবিধা হচ্ছে — আপনাকে একজন এজেন্টের সাথে সংযুক্ত করছি।",
}


def clarification_prompt(action: str, language: str | None) -> str:
    code = (language or "en").strip().lower()[:2]
    if action == CLARIFY_OFFER_OPTIONS:
        return _CLARIFY_OPTIONS.get(code, _CLARIFY_OPTIONS["en"])
    if action == CLARIFY_ESCALATE:
        return _CLARIFY_ESCALATE.get(code, _CLARIFY_ESCALATE["en"])
    return _CLARIFY_NUDGE.get(code, _CLARIFY_NUDGE["en"])


# ── Turn arbitration ────────────────────────────────────────────────────────


TURN_IDLE = "idle"
TURN_THINKING = "thinking"     # routing + LLM stream, no audio yet
TURN_SPEAKING = "speaking"     # first TTS audio has been sent
TURN_DONE = "done"


@dataclass
class _Turn:
    turn_id: str
    started_at: float
    task: asyncio.Task[Any] | None = None
    tts_pump: Any | None = None
    phase: str = TURN_IDLE
    cancelled: bool = False
    speaking_at: float | None = None


class TurnArbiter:
    """Owns the lifecycle of the current turn and arbitrates incoming
    utterances against the in-flight one.

    Replaces the previous ``current_turn`` / ``turn_state["speaking"]``
    mutable pair with explicit phase transitions:

    * :data:`TURN_IDLE` — no turn running.
    * :data:`TURN_THINKING` — turn started; routing / retrieval / LLM
      stream in progress but no audio sent yet.
    * :data:`TURN_SPEAKING` — first TTS audio packet has been dispatched.
      A new utterance here is a real barge-in.
    * :data:`TURN_DONE` — turn finished or cancelled.

    The arbiter exposes :meth:`classify_incoming` which returns one of
    ``proceed`` / ``check_in`` / ``barge_in``. The voice stream service
    uses that verdict to decide whether to:

    * proceed normally (no turn in flight),
    * keep the queued reply alive and play a "yes, here" ack (caller is
      checking the line while we're still composing),
    * cancel the in-flight turn + TTS pump (caller is genuinely
      interrupting).
    """

    def __init__(self) -> None:
        self._current: _Turn | None = None

    @property
    def phase(self) -> str:
        return self._current.phase if self._current else TURN_IDLE

    @property
    def is_active(self) -> bool:
        return self._current is not None and self._current.phase not in (TURN_DONE,)

    @property
    def current_turn_id(self) -> str | None:
        return self._current.turn_id if self._current else None

    def begin(self, turn_id: str, task: asyncio.Task[Any] | None = None) -> None:
        self._current = _Turn(turn_id=turn_id, started_at=perf_counter(), task=task, phase=TURN_THINKING)

    def attach_pump(self, pump: Any) -> None:
        if self._current is not None:
            self._current.tts_pump = pump

    def mark_speaking(self) -> None:
        if self._current is not None and self._current.phase == TURN_THINKING:
            self._current.phase = TURN_SPEAKING
            self._current.speaking_at = perf_counter()

    def mark_done(self) -> None:
        if self._current is not None:
            self._current.phase = TURN_DONE

    def classify_incoming(self, *, is_check_in: bool) -> str:
        """Decide how a freshly-arrived utterance should be handled.

        Returns one of:

        * ``"proceed"`` — no turn in flight (or the in-flight one already
          completed). The caller is taking a new turn cleanly.
        * ``"check_in"`` — turn is in flight but hasn't started speaking
          yet, and the caller said something check-in-shaped (
          ``"hello, are you there?"``). Keep the queued answer running
          and play a short ack on top.
        * ``"barge_in"`` — turn is actively speaking. Cancel it.
        """
        if not self.is_active or self._current is None:
            return "proceed"
        # Done tasks count as no-turn.
        if self._current.task is not None and self._current.task.done():
            return "proceed"
        if self._current.phase == TURN_SPEAKING:
            return "barge_in"
        # Still thinking. A check-in-shaped utterance is the only
        # benign interruption here.
        if is_check_in:
            return "check_in"
        return "barge_in"

    async def cancel(self) -> None:
        """Cancel the in-flight turn (LLM stream + TTS pump) and wait for
        it to fully unwind. Safe to call when no turn is in flight."""
        cur = self._current
        if cur is None:
            return
        cur.cancelled = True
        # Cancel the pump first so we don't keep firing TTS at the
        # speaker while waiting for the streaming LLM to notice it's
        # been cancelled.
        if cur.tts_pump is not None:
            try:
                cancel_fn = getattr(cur.tts_pump, "cancel", None)
                if cancel_fn is not None:
                    await cancel_fn()
            except Exception:
                pass
        if cur.task is not None and not cur.task.done():
            cur.task.cancel()
            try:
                await cur.task
            except (asyncio.CancelledError, Exception):
                pass
        cur.phase = TURN_DONE


# ── Vague-turn classifier ───────────────────────────────────────────────────


_VAGUE_MIN_WORDS = 2


def is_turn_vague(
    *,
    route: str,
    intent: str | None,
    refused: bool,
    chunks: Iterable[Any] | None,
    user_text: str,
    state_slot: str | None = None,
) -> bool:
    """Decide whether the just-completed turn counts as 'vague' for the
    purposes of the clarification FSM.

    A turn is vague when ALL of the following hold:

    * The route did not match a deterministic branch (template / answer
      card / policy card / tool flow), AND
    * Either no retrieval chunks were found, or the pipeline returned a
      refusal, AND
    * The caller's text was short or carried no recognised intent.

    Conversation-flow turns (state_slot set, intent is a known sensitive
    or actionable label) are NEVER counted as vague: those are the FSM
    asking a slot question, not the agent failing to understand.
    """
    if state_slot:
        return False
    if route in {"template", "answer_card", "policy_card", "smalltalk_llm"}:
        # Templates and policy cards always answer something concrete.
        return False
    if intent and intent not in {"unknown_general", None}:
        # We knew what the caller wanted, even if we didn't have an
        # answer — that's a knowledge-base miss, not a vagueness issue.
        return False
    if not refused and chunks and any(chunks):
        return False
    if user_text and len(user_text.split()) >= _VAGUE_MIN_WORDS and ("?" in user_text or "।" in user_text):
        # The caller asked a specific question we just couldn't ground —
        # KB miss, again, not vagueness. The standard refusal handles it.
        return False
    return True


# ── Public registry helper ──────────────────────────────────────────────────


@dataclass
class RobustnessContext:
    """Bundle of robustness primitives shared across the call's lifetime.

    The voice stream service holds one of these per active WebSocket and
    threads it through the per-turn callbacks. Each primitive is
    independently usable so unit tests can exercise them in isolation.
    """

    language_state: LanguageState = field(default_factory=LanguageState)
    arbiter: TurnArbiter = field(default_factory=TurnArbiter)
    # The clarification state is hydrated from / persisted to the Redis
    # session blob; the arbiter and language state are process-local.
    clarification: ClarificationState = field(default_factory=ClarificationState)

    def hydrate_clarification(self, session_state: dict[str, Any] | None) -> None:
        if not isinstance(session_state, dict):
            return
        block = session_state.get("clarification")
        if isinstance(block, dict):
            self.clarification = ClarificationState.from_dict(block)


# Convenience type alias for the recovery callback the stream service
# passes to the audio probe handler. The robustness layer calls back
# into the WebSocket via this signature when it wants to dispatch a
# repeat-prompt or escalation reply.
RecoveryDispatcher = Callable[[str, str], Awaitable[None]]


__all__ = [
    "AudioQuality",
    "AudioQualityProbe",
    "ClarificationState",
    "LanguageState",
    "RobustnessContext",
    "TurnArbiter",
    "TURN_IDLE",
    "TURN_THINKING",
    "TURN_SPEAKING",
    "TURN_DONE",
    "QUALITY_OK",
    "QUALITY_POOR",
    "QUALITY_UNUSABLE",
    "CLARIFY_RESET",
    "CLARIFY_NUDGE",
    "CLARIFY_OFFER_OPTIONS",
    "CLARIFY_ESCALATE",
    "clarification_prompt",
    "is_turn_vague",
    "repeat_prompt",
    "RecoveryDispatcher",
]
