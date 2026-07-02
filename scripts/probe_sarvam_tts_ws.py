"""Probe Sarvam's WebSocket TTS to pin the exact message shapes BEFORE the flip.

The WS engine typically blocks waiting for more text until it receives the EXACT
end-of-input / flush message. The wrong shape → the socket hangs → in production
our first-audio watchdog trips and we silently fall back to REST (win lost, no
error). So this probe HAMMERS the flush variants: for each, it opens a fresh WS,
sends config + text + that flush, reads frames, and reports whether audio arrived,
how many frames, connect ms, first-audio ms, and whether the socket closed cleanly.

Whichever shape reliably yields COMPLETE audio is what `synthesize_streaming_ws`
should send (update the flush there + the parser if a frame shape differs).

Run from an allowlisted env (needs settings.SARVAM_API_KEY or tenant creds):
    source venv/bin/activate
    python3 scripts/probe_sarvam_tts_ws.py "Hello, this is a test of streaming."
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from time import perf_counter
from types import SimpleNamespace

from app.core.config import settings
from app.services.sarvam_voice_service import SarvamVoiceService

# Flush / end-of-input candidates, tried in order. Extend freely — the point is to
# find the ONE Sarvam honours. `None` = send no flush (rely on the engine to
# synthesize on its own / on close) as a control.
_FLUSH_VARIANTS: list[tuple[str, dict | None]] = [
    ("type=flush", {"type": "flush"}),
    ("flush=true", {"flush": True}),
    ("type=eof", {"type": "eof"}),
    ("type=end", {"type": "end"}),
    ("empty-text-terminal", {"type": "text", "data": {"text": ""}}),
    ("no-flush", None),
]

# Config / text envelope. If audio never arrives for ANY flush, try flipping these
# to the alternates in comments (Sarvam has used both flat and data-wrapped shapes).
def _config_msg(cfg: dict) -> dict:
    return {"type": "config", "data": cfg}          # alt: {"type": "config", **cfg}


def _text_msg(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}  # alt: {"type": "text", "text": text}


READ_BUDGET_S = 8.0  # per-variant: how long to wait for audio before giving up


def _build_config(tenant, language: str | None) -> dict:
    provider_status = dict(tenant.provider_status or {})
    model = provider_status.get("sarvam_tts_model") or settings.SARVAM_TTS_MODEL
    cfg = {
        "target_language_code": SarvamVoiceService.to_bcp47(language),
        "speaker": SarvamVoiceService.tts_speaker_for(language, provider_status),
        "model": model,
        "speech_sample_rate": int(settings.SARVAM_TTS_SAMPLE_RATE),
        "output_audio_codec": settings.SARVAM_TTS_WS_OUTPUT_CODEC,
        "enable_cached_responses": False,
    }
    if model == "bulbul:v3":
        cfg["temperature"] = 0.6
    pace = SarvamVoiceService.pace_for(language, None)
    if pace is not None:
        cfg["pace"] = max(0.3, min(3.0, float(pace)))
    return cfg


async def _try_variant(tenant, text: str, language: str | None, name: str, flush: dict | None) -> dict:
    result = {"variant": name, "connect_ms": None, "first_audio_ms": None,
              "frames": 0, "bytes": 0, "clean_close": False, "error": None, "types": []}
    t0 = perf_counter()
    try:
        ws = await asyncio.wait_for(SarvamVoiceService.connect_tts_ws(tenant), timeout=4.0)
    except Exception as exc:
        result["error"] = f"connect failed: {exc!r}"
        return result
    result["connect_ms"] = int((perf_counter() - t0) * 1000)
    try:
        await ws.send(json.dumps(_config_msg(_build_config(tenant, language))))
        await ws.send(json.dumps(_text_msg(text)))
        if flush is not None:
            await ws.send(json.dumps(flush))
        deadline = perf_counter() + READ_BUDGET_S
        while perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - perf_counter())
            except asyncio.TimeoutError:
                break
            msg = SarvamVoiceService._parse_tts_ws_message(raw)
            if msg is None:
                # Show a snippet of any unparseable frame — helps spot a new shape.
                result["types"].append("UNPARSEABLE:" + str(raw)[:80])
                continue
            if msg.get("type"):
                result["types"].append(msg["type"])
            if msg.get("error"):
                result["error"] = str(msg["error"])[:200]
                break
            for a in msg.get("audios") or []:
                result["frames"] += 1
                try:
                    result["bytes"] += len(base64.b64decode(a))
                except Exception:
                    pass
                if result["first_audio_ms"] is None:
                    result["first_audio_ms"] = int((perf_counter() - t0) * 1000)
            if msg.get("done"):
                result["clean_close"] = True
                break
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        try:
            await ws.close()
        except Exception:
            pass
    return result


async def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "Hello, this is a test of streaming text to speech."
    language = sys.argv[2] if len(sys.argv) > 2 else "en"
    if not settings.SARVAM_API_KEY:
        print("WARN: settings.SARVAM_API_KEY empty — connect will need tenant creds and likely fail.")
    tenant = SimpleNamespace(provider_status={})
    print(f"URL: {SarvamVoiceService.tts_websocket_url(tenant)}")
    print(f"codec={settings.SARVAM_TTS_WS_OUTPUT_CODEC} rate={settings.SARVAM_TTS_SAMPLE_RATE} lang={language}")
    print(f"text: {text!r}\n")
    print(f"{'variant':22} {'connect':>8} {'1st-aud':>8} {'frames':>7} {'bytes':>8} {'clean':>6}  types/error")
    print("-" * 100)
    winners: list[str] = []
    for name, flush in _FLUSH_VARIANTS:
        r = await _try_variant(tenant, text, language, name, flush)
        types = ",".join(dict.fromkeys(r["types"]))[:40]
        detail = r["error"] or types
        print(f"{name:22} {str(r['connect_ms']):>8} {str(r['first_audio_ms']):>8} "
              f"{r['frames']:>7} {r['bytes']:>8} {str(r['clean_close']):>6}  {detail}")
        if r["frames"] > 0:
            winners.append(name)
    print("-" * 100)
    if winners:
        print(f"\n✅ flush shapes that produced audio: {winners}")
        print("   → set synthesize_streaming_ws's flush to the first of these; confirm the")
        print("     reported sample rate matches SARVAM_TTS_SAMPLE_RATE (else audio will pitch-shift).")
        return 0
    print("\n❌ NO variant produced audio. Try the alternate config/text envelopes in "
          "_config_msg/_text_msg, or check the API key / model.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
