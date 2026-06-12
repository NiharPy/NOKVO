"""Inbound audio conditioning for STT: AGC enhancer + Plivo rate plumbing.

Pins the no-pump / no-clip / no-noise-amplification invariants of AudioEnhancer
and that the Plivo bridge drives the sample rate from the stream's real format.
"""
from __future__ import annotations

import numpy as np

from app.services.agent_robustness import AudioEnhancer
from app.services.plivo_bridge_service import PlivoWebSocketAdapter
from app.services.twilio_bridge_service import _resample

FS = 32767.0


def _sine(amp: float, n: int = 1600, freq: int = 220, rate: int = 16000) -> bytes:
    t = np.arange(n) / rate
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16).tobytes()


def _rms(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean((x / FS) ** 2))) if x.size else 0.0


def _maxabs(pcm: bytes) -> int:
    return int(np.abs(np.frombuffer(pcm, dtype=np.int16)).max())


# ── AudioEnhancer ───────────────────────────────────────────────────────────


def test_quiet_audio_is_boosted_toward_target_without_clipping():
    enh = AudioEnhancer(target_dbfs=-20.0)  # target rms ~0.1
    quiet = _sine(700)  # ~-30 dBFS
    in_rms = _rms(quiet)
    # Feed several chunks so the smoothed AGC converges.
    out = quiet
    for _ in range(12):
        out = enh.process(quiet)
    assert _rms(out) > in_rms * 2          # materially louder
    assert _rms(out) <= 0.12               # near target, not blown past it
    assert _maxabs(out) < int(FS)          # never clips to full scale


def test_loud_audio_is_not_boosted_or_clipped():
    enh = AudioEnhancer(target_dbfs=-20.0)
    loud = _sine(20000)  # well above target
    out = loud
    for _ in range(12):
        out = enh.process(loud)
    assert enh.gain <= 1.0
    assert _maxabs(out) <= int(FS)         # no new clipping introduced


def test_dc_offset_is_removed():
    enh = AudioEnhancer()
    n = 1600
    dc = (np.ones(n) * 6000 + np.sin(np.linspace(0, 8 * np.pi, n)) * 1500).astype(np.int16).tobytes()
    out = np.frombuffer(enh.process(dc), dtype=np.int16).astype(np.float32)
    assert abs(float(out.mean())) < 50     # DC bias gone


def test_silence_is_not_amplified():
    enh = AudioEnhancer()
    silence = np.zeros(1600, dtype=np.int16).tobytes()
    for _ in range(5):
        out = enh.process(silence)
    assert _maxabs(out) == 0
    assert enh.gain == 1.0                  # never chased a gain on pure silence


def test_gain_does_not_pump_on_a_single_loud_burst():
    # A quiet stream that converges, then one loud chunk: the release is slow,
    # so the gain must not collapse in a single step (which would pump audibly).
    enh = AudioEnhancer(target_dbfs=-20.0)
    for _ in range(15):
        enh.process(_sine(700))
    g_before = enh.gain
    enh.process(_sine(20000))               # one loud burst
    # Released downward but only partially — not a hard snap to ~1.0.
    assert enh.gain < g_before
    assert enh.gain > 1.0


def test_process_is_best_effort_on_garbage():
    enh = AudioEnhancer()
    assert enh.process(b"") == b""
    # Odd byte count (not whole int16) must not raise.
    enh.process(b"\x01\x02\x03")


# ── Resampler anti-aliasing ─────────────────────────────────────────────────


def test_resample_equal_rate_is_noop():
    s = np.frombuffer(_sine(1000), dtype=np.int16)
    assert _resample(s, 16000, 16000) is s


def test_resample_downsample_preserves_length_ratio():
    s = np.frombuffer(_sine(1000, n=1600), dtype=np.int16)
    out = _resample(s, 16000, 8000)
    assert abs(len(out) - 800) <= 1         # ~half the samples, anti-aliased


# ── Plivo bridge: drive rate from the stream's real media format ────────────


class _FakeWS:
    async def accept(self):
        return None


def test_bridge_reads_real_sample_rate_from_start_event():
    a = PlivoWebSocketAdapter(_FakeWS(), language="en")
    # 8 kHz declared in the start media format → adopt it (and resample 8→16k).
    rate, _enc = PlivoWebSocketAdapter._detect_stream_format(
        {}, {"mediaFormat": {"sampleRate": 8000}}
    )
    assert rate == 8000
    # Alternate key shapes Plivo has used.
    assert PlivoWebSocketAdapter._detect_stream_format({}, {"media_format": {"rate": 16000}})[0] == 16000
    assert PlivoWebSocketAdapter._detect_stream_format({}, {"sampleRate": 16000})[0] == 16000
    # Nonsense / missing → None (caller keeps the default).
    assert PlivoWebSocketAdapter._detect_stream_format({}, {})[0] is None
    assert PlivoWebSocketAdapter._detect_stream_format({}, {"mediaFormat": {"sampleRate": "junk"}})[0] is None


def test_bridge_detects_stream_encoding():
    # Default is l16 (what our answer XML requests) — µ-law only when the
    # start event explicitly says so. Decoding L16 as µ-law is pure noise.
    a = PlivoWebSocketAdapter(_FakeWS(), language="en")
    assert a._source_encoding == "l16"
    detect = PlivoWebSocketAdapter._detect_stream_format
    assert detect({}, {"mediaFormat": {"encoding": "audio/x-mulaw"}})[1] == "mulaw"
    assert detect({}, {"mediaFormat": {"contentType": "audio/x-l16;rate=16000"}})[1] == "l16"
    assert detect({}, {"media_format": {"encoding": "ulaw"}})[1] == "mulaw"
    assert detect({}, {})[1] is None  # unknown → caller keeps the l16 default


def test_bridge_decodes_l16_without_ulaw_garbling():
    # A pure L16 sine processed at equal rates must come out looking like the
    # input (AGC may scale it) — NOT µ-law-decoded noise at double duration.
    a = PlivoWebSocketAdapter(_FakeWS(), language="en")
    a._source_rate = 16000
    a._input_rate = 16000
    a._denoiser = None  # isolate the decode path from the RNNoise stage
    pcm = _sine(8000, n=1600, freq=440, rate=16000)
    out = a._process_inbound(pcm)
    assert len(out) == len(pcm)  # sample count preserved (µ-law would double it)
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    y = np.frombuffer(out, dtype=np.int16).astype(np.float32)
    # Shape preserved: normalized correlation stays high (the ~100 Hz HPF
    # phase-shifts slightly; µ-law-garbled audio would correlate near 0).
    corr = float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9))
    assert corr > 0.9


def test_bridge_process_inbound_resamples_8k_to_stt_rate():
    a = PlivoWebSocketAdapter(_FakeWS(), language="en")
    a._source_rate = 8000               # as if the start event said 8 kHz
    a._input_rate = 16000               # Sarvam STT rate (run_session aligns this)
    pcm8k = _sine(4000, n=800, rate=8000)   # 800 samples @ 8k = 100 ms
    out = a._process_inbound(pcm8k)
    # Resampled up to the STT input rate (16k) → ~2x the samples.
    assert abs(len(np.frombuffer(out, dtype=np.int16)) - 1600) <= 2


def test_nokvo_one_path_aligns_adapter_rate_to_sarvam_stt(monkeypatch):
    # The real bug: with AGENT_VOICE_BACKEND=azure_realtime the adapter seeds
    # _input_rate=24000, but Nokvo One runs Sarvam STT at 16000 → 1.5x garble.
    # run_session must realign the adapter to the Sarvam rate.
    import asyncio
    from types import SimpleNamespace
    from app.core.config import settings
    from app.services.plivo_bridge_service import PlivoBridgeService
    import app.services.nokvo_one_voice_stream_service as svc

    monkeypatch.setattr(settings, "AGENT_VOICE_BACKEND", "azure_realtime")
    captured = {}

    async def _fake_run(adapter, tenant_res, *, db=None, language="en"):
        captured["rate"] = adapter._input_rate

    monkeypatch.setattr(svc.NokvoOneVoiceStreamService, "run_session", staticmethod(_fake_run))
    tenant = SimpleNamespace(provider_status={"product_tier": "nokvo_one"}, call_context={})

    class _WS:
        async def accept(self):
            return None

    asyncio.new_event_loop().run_until_complete(
        PlivoBridgeService.run_session(_WS(), tenant, db=None, language="en")
    )
    assert captured["rate"] == int(settings.SARVAM_STT_SAMPLE_RATE) == 16000


# ── SpeechDenoiser (RNNoise) ─────────────────────────────────────────────────


def _denoise_available() -> bool:
    from app.services.audio_denoise import denoise_available
    return denoise_available()


def test_denoiser_passthrough_when_unavailable(monkeypatch):
    # Import failure → feature disables itself; audio passes through untouched.
    import app.services.audio_denoise as dn

    monkeypatch.setattr(dn, "_rnnoise_mod", None)
    monkeypatch.setattr(dn, "_rnnoise_failed", True)
    d = dn.SpeechDenoiser()
    assert not d.available
    pcm = _sine(4000, n=320, rate=16000)
    assert d.process(pcm, rate=16000) == pcm
    d.close()  # idempotent, no raise


def test_denoiser_conserves_samples_over_stream():
    import pytest

    if not _denoise_available():
        pytest.skip("pyrnnoise not available")
    from app.services.audio_denoise import SpeechDenoiser

    d = SpeechDenoiser()
    assert d.available
    total_in = 0
    total_out = 0
    # 50 × 20 ms chunks @ 16 kHz (320 samples each).
    for _ in range(50):
        pcm = _sine(6000, n=320, rate=16000)
        total_in += len(pcm) // 2
        out = d.process(pcm, rate=16000)
        total_out += len(out) // 2
        # No clipping ever.
        if out:
            assert _maxabs(out) <= 32767
    d.close()
    # Stream-wide sample conservation: at most one 10 ms frame (160 samples
    # at 16 kHz) may be held in the carry buffer at any time.
    assert total_in - total_out <= 161


def test_denoiser_latency_budget():
    import time

    import pytest

    if not _denoise_available():
        pytest.skip("pyrnnoise not available")
    from app.services.audio_denoise import SpeechDenoiser

    d = SpeechDenoiser()
    pcm = _sine(6000, n=320, rate=16000)  # one 20 ms chunk
    # Warm-up (lazy imports, first-frame state).
    for _ in range(5):
        d.process(pcm, rate=16000)
    timings = []
    for _ in range(50):
        start = time.perf_counter()
        d.process(pcm, rate=16000)
        timings.append(time.perf_counter() - start)
    d.close()
    timings.sort()
    median_ms = timings[len(timings) // 2] * 1000
    # Latency-neutral requirement: well under the 30 ms chunk budget.
    assert median_ms < 5.0, f"denoise too slow: {median_ms:.2f} ms per 20 ms chunk"


def test_denoiser_attenuates_pure_noise():
    import pytest

    if not _denoise_available():
        pytest.skip("pyrnnoise not available")
    from app.services.audio_denoise import SpeechDenoiser

    d = SpeechDenoiser()
    rng = np.random.default_rng(7)
    in_rms_total, out_rms_total = 0.0, 0.0
    for _ in range(50):  # 1 s of pure noise
        noise = (rng.standard_normal(320) * 3000).astype(np.int16).tobytes()
        out = d.process(noise, rate=16000)
        in_rms_total += _rms(noise)
        if out:
            out_rms_total += _rms(out)
    d.close()
    # Speech-free input should come out materially quieter.
    assert out_rms_total < in_rms_total * 0.6
    assert d.last_speech_prob < 0.5


def test_bridge_chain_order_resample_denoise_enhance():
    # _process_inbound must run: decode → resample → denoise → AGC. The AGC
    # must never see (and amplify) the noise floor the denoiser removes.
    calls = []

    class _RecDenoiser:
        last_speech_prob = 0.0

        def process(self, pcm, rate=16000):
            calls.append("denoise")
            return pcm

        def close(self):
            pass

    class _RecEnhancer:
        last_rms = 0.0
        gain = 1.0

        def process(self, pcm):
            calls.append("enhance")
            return pcm

    a = PlivoWebSocketAdapter(_FakeWS(), language="en")
    a._source_rate = 16000
    a._input_rate = 16000
    a._denoiser = _RecDenoiser()
    a._enhancer = _RecEnhancer()
    a._process_inbound(_sine(4000, n=320, rate=16000))
    assert calls == ["denoise", "enhance"]


def test_adapter_close_audio_releases_denoiser():
    closed = []

    class _RecDenoiser:
        def close(self):
            closed.append(True)

    a = PlivoWebSocketAdapter(_FakeWS(), language="en")
    a._denoiser = _RecDenoiser()
    a.close_audio()
    assert closed == [True]
    assert a._denoiser is None
    a.close_audio()  # idempotent


# ── Enhancer upgrades: high-pass, soft limiter, adaptive noise cap ──────────


def test_highpass_kills_rumble_passes_voice():
    # Mix 50 Hz rumble + 300 Hz voice in ONE stream (so the AGC applies the
    # same gain to both components) and compare their spectral energy ratio
    # before vs after: the rumble bin must drop relative to the voice bin.
    from app.services.agent_robustness import AudioEnhancer

    rate, n = 16000, 3200
    t = np.arange(n) / rate
    mix = ((np.sin(2 * np.pi * 50 * t) + np.sin(2 * np.pi * 300 * t)) * 6000).astype(np.int16)
    enh = AudioEnhancer(target_dbfs=-20.0)
    out = mix.tobytes()
    for _ in range(5):
        out = enh.process(mix.tobytes())
    y = np.frombuffer(out, dtype=np.int16).astype(np.float64)

    def _bin_mag(sig: np.ndarray, freq: int) -> float:
        spectrum = np.abs(np.fft.rfft(sig))
        return float(spectrum[int(round(freq * n / rate))])

    in_ratio = _bin_mag(mix.astype(np.float64), 50) / _bin_mag(mix.astype(np.float64), 300)
    out_ratio = _bin_mag(y, 50) / _bin_mag(y, 300)
    # First-order HPF at ~100 Hz: 50 Hz down ~7 dB, 300 Hz nearly untouched.
    assert out_ratio < in_ratio * 0.6


def test_soft_limiter_no_hard_clip_on_burst():
    from app.services.agent_robustness import AudioEnhancer

    enh = AudioEnhancer(target_dbfs=-20.0)
    # Converge on quiet audio (gain rises), then a burst that overshoots the
    # ceiling moderately (~1.2× full scale after gain).
    for _ in range(15):
        enh.process(_sine(700))
    burst_amp = int((FS * 1.2) / max(enh.gain, 1.0))
    out = enh.process(_sine(burst_amp))
    x = np.frombuffer(out, dtype=np.int16)
    peak = int(np.abs(x).max())
    knee = int(AudioEnhancer.LIMITER_KNEE * FS)
    # Ceiling respected, and strictly BELOW full scale — a hard np.clip would
    # pin the peak at exactly 32767; tanh compression rounds it under.
    assert knee < peak < 32767
    # No samples square-pinned at the ceiling.
    assert float(np.mean(np.abs(x) >= 32760)) == 0.0


def test_noise_floor_cap_limits_gain_on_hissy_line():
    from app.services.agent_robustness import AudioEnhancer

    rng = np.random.default_rng(3)
    enh = AudioEnhancer(target_dbfs=-20.0)
    # Teach the tracker a loud between-words floor: near-silence chunks at
    # ~3x the silence threshold (audible hiss).
    floor_amp = int(AudioEnhancer.SILENCE_RMS * 2.5 * 32767)
    for _ in range(20):
        hiss = (rng.standard_normal(320) * floor_amp).astype(np.int16).tobytes()
        enh.process(hiss)
    assert enh._noise_floor > AudioEnhancer.SILENCE_RMS
    # Now quiet speech arrives: gain must stay capped well below MAX_GAIN.
    for _ in range(12):
        enh.process(_sine(700))
    capped_gain = enh.gain
    # Same speech on a clean line converges to a much higher gain.
    enh_clean = AudioEnhancer(target_dbfs=-20.0)
    for _ in range(12):
        enh_clean.process(_sine(700))
    assert capped_gain < enh_clean.gain * 0.7


# ── Leading-silence trim ─────────────────────────────────────────────────────


def test_trim_leading_silence_keeps_preroll():
    from app.services.agent_robustness import trim_leading_silence

    rate = 16000
    silence = b"\x00\x00" * rate            # 1 s of digital silence
    speech = _sine(8000, n=rate // 2, rate=rate)  # 0.5 s of tone
    trimmed = trim_leading_silence(silence + speech, sample_rate=rate, preroll_ms=100)
    # Most of the dead second is gone, but ~100 ms pre-roll plus all speech stay.
    kept_ms = (len(trimmed) // 2) / rate * 1000
    assert 500 <= kept_ms <= 700


def test_trim_leading_silence_never_trims_all_silence_or_clean_speech():
    from app.services.agent_robustness import trim_leading_silence

    rate = 16000
    all_silence = b"\x00\x00" * rate
    assert trim_leading_silence(all_silence, sample_rate=rate) == all_silence
    speech = _sine(8000, n=rate, rate=rate)
    assert trim_leading_silence(speech, sample_rate=rate) == speech
    assert trim_leading_silence(b"", sample_rate=rate) == b""
