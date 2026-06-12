"""RNNoise speech denoise for the caller→STT path.

Telephony audio arrives with line hiss, street noise, and babble that the
AGC would otherwise amplify and STT then mishears. RNNoise is a small
speech-trained NN (10 ms frames @ 48 kHz, ~0.9 ms CPU per frame here) that
suppresses non-speech energy while leaving the voice intact — applied
BEFORE the AGC so gain never boosts the noise floor the denoiser removes.

Loading strategy: ``pyrnnoise``'s package ``__init__`` drags in heavy AV
dependencies (PyAV/audiolab) whose API drifted, so we load its pure-ctypes
submodule (``pyrnnoise/rnnoise.py`` — needs only numpy + the bundled
librnnoise) directly via importlib, bypassing ``__init__``. Any failure
anywhere disables the feature for the process with ONE warning; audio then
passes through untouched. A denoiser instance is per-call (RNNoise state is
stateful across frames) and must be ``close()``d at session end.

Rate handling: the bridge hands us 16 kHz PCM; 16→48 is an exact ×3
upsample and 48→16 an exact ÷3 downsample through the existing numpy
``_resample`` (which anti-aliases on the way down). A carry buffer holds
the sub-frame remainder (<10 ms) between chunks so frame boundaries never
drop samples.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_RNNOISE_RATE = 48000

# Process-level latch: None = not tried yet, False = unavailable (warned),
# module object = ready.
_rnnoise_mod: Any = None
_rnnoise_failed = False


def _load_rnnoise() -> Any:
    """Load pyrnnoise's ctypes layer, bypassing its heavy package __init__."""
    global _rnnoise_mod, _rnnoise_failed
    if _rnnoise_mod is not None:
        return _rnnoise_mod
    if _rnnoise_failed:
        return None
    try:
        # Preferred: normal import (works when the package's deps are intact).
        from pyrnnoise import rnnoise as mod  # type: ignore

        _rnnoise_mod = mod
        return mod
    except Exception:
        pass
    try:
        import importlib.util
        import os

        spec_pkg = importlib.util.find_spec("pyrnnoise")
        if spec_pkg is None or not spec_pkg.submodule_search_locations:
            raise ImportError("pyrnnoise not installed")
        path = os.path.join(list(spec_pkg.submodule_search_locations)[0], "rnnoise.py")
        spec = importlib.util.spec_from_file_location("_nokvo_rnnoise_ctypes", path)
        if spec is None or spec.loader is None:
            raise ImportError("pyrnnoise/rnnoise.py not loadable")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _rnnoise_mod = mod
        return mod
    except Exception as exc:
        _rnnoise_failed = True
        logger.warning(
            "VOICE-DENOISE: RNNoise unavailable (%s) — caller audio passes through "
            "without denoise. `pip install pyrnnoise` to enable.", exc,
        )
        return None


def denoise_available() -> bool:
    return _load_rnnoise() is not None


class SpeechDenoiser:
    """Per-call streaming RNNoise wrapper. Best-effort: any runtime failure
    returns the input unchanged so a DSP glitch never drops caller audio."""

    def __init__(self) -> None:
        self._mod = _load_rnnoise()
        self._state = None
        self._carry = None  # leftover 48 kHz samples (< one frame)
        self._last_speech_prob = 0.0
        if self._mod is not None:
            try:
                self._state = self._mod.create()
            except Exception:
                logger.warning("VOICE-DENOISE: rnnoise state create failed", exc_info=True)
                self._mod = None

    @property
    def available(self) -> bool:
        return self._state is not None

    @property
    def last_speech_prob(self) -> float:
        """Speech probability of the most recent processed frame (0..1)."""
        return self._last_speech_prob

    def process(self, pcm: bytes, rate: int = 16000) -> bytes:
        """Denoise one buffer of signed 16-bit LE mono PCM at ``rate``.

        Output sample count equals input sample count over the stream as a
        whole; up to one 10 ms frame of audio is carried between calls.
        """
        if not pcm or self._state is None:
            return pcm
        try:
            import numpy as np

            from app.services.twilio_bridge_service import _resample

            usable = len(pcm) - (len(pcm) % 2)
            x16 = np.frombuffer(pcm[:usable], dtype=np.int16)
            if x16.size == 0:
                return pcm
            x48 = _resample(x16, rate, _RNNOISE_RATE)
            if self._carry is not None and self._carry.size:
                x48 = np.concatenate([self._carry, x48])
            frame_size = int(self._mod.FRAME_SIZE)
            n_frames = x48.size // frame_size
            if n_frames == 0:
                self._carry = x48
                return b""
            head, self._carry = x48[: n_frames * frame_size], x48[n_frames * frame_size:]
            out_frames = []
            for i in range(n_frames):
                frame = np.ascontiguousarray(head[i * frame_size:(i + 1) * frame_size])
                denoised, prob = self._mod.process_mono_frame(self._state, frame)
                self._last_speech_prob = float(prob)
                out_frames.append(denoised)
            y48 = np.concatenate(out_frames)
            y16 = _resample(y48, _RNNOISE_RATE, rate)
            return y16.astype(np.int16).tobytes()
        except Exception:
            logger.debug("VOICE-DENOISE: process failed — passing through", exc_info=True)
            return pcm

    def close(self) -> None:
        if self._state is not None and self._mod is not None:
            try:
                self._mod.destroy(self._state)
            except Exception:
                pass
        self._state = None


__all__ = ("SpeechDenoiser", "denoise_available")
