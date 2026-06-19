#!/usr/bin/env python3
"""Synthetic Plivo-media load harness — calibrate calls-per-replica.

Opens N concurrent media WebSockets to the running API exactly the way Plivo
does (a `start` event, then base64 L16 PCM `media` frames at real-time pace),
and measures **connect → first agent audio** latency plus playback continuity
under load. Use it against a STAGING deployment to find the per-replica knee,
then set the Container Apps `concurrentRequests` scaler target from the result.

It speaks the protocol in app/services/plivo_bridge_service.py:
  send:  {"event":"start", ...}
         {"event":"media","media":{"track":"inbound","payload":<b64 L16>}}
         {"event":"stop"}
  recv:  {"event":"playAudio","media":{...}}   # agent TTS
         {"event":"clearAudio"}                # barge-in

Requires a valid tenant phone-link id (the {link_id} in the media URL) so the
server resolves a tenant. Audio is optional: with --audio <wav>, real speech is
streamed (measures a true turn); without it, a tone is sent and you still get
greeting latency + connection capacity.

Example:
  python plivo_load.py --url wss://api.staging.example \
    --link-id <LINK_ID> --calls 20 --ramp 5 --audio sample_16k.wav --hold 8
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import time
import wave

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is a project dep
    np = None

try:
    import websockets
except Exception as exc:  # pragma: no cover
    raise SystemExit("pip install websockets") from exc

FRAME_MS = 20  # Plivo streams ~20ms frames


def load_pcm(path: str | None, rate: int, seconds: float) -> bytes:
    """Return mono 16-bit little-endian PCM at `rate`. From a WAV file, or a
    generated tone when no file is given."""
    if path:
        with wave.open(path, "rb") as wf:
            assert wf.getsampwidth() == 2, "WAV must be 16-bit PCM"
            data = wf.readframes(wf.getnframes())
            if wf.getframerate() != rate and np is not None:
                src = np.frombuffer(data, dtype=np.int16)
                if wf.getnchannels() == 2:
                    src = src.reshape(-1, 2).mean(axis=1).astype(np.int16)
                # cheap linear resample to the target rate
                n_out = int(len(src) * rate / wf.getframerate())
                xp = np.linspace(0, len(src), num=len(src), endpoint=False)
                x = np.linspace(0, len(src), num=n_out, endpoint=False)
                data = np.interp(x, xp, src).astype(np.int16).tobytes()
            return data
    # Tone fallback: a quiet 220Hz sine so frames aren't pure silence.
    if np is None:
        return (b"\x00\x00" * int(rate * seconds))
    t = np.arange(int(rate * seconds)) / rate
    tone = (np.sin(2 * np.pi * 220 * t) * 3000).astype(np.int16)
    return tone.tobytes()


def frames(pcm: bytes, rate: int):
    step = int(rate * FRAME_MS / 1000) * 2  # bytes per frame (2 bytes/sample)
    for i in range(0, len(pcm) - step + 1, step):
        yield pcm[i:i + step]


async def run_call(idx: int, url: str, link_id: str, caller: str, pcm: bytes,
                   rate: int, hold: float, results: list) -> None:
    media_url = f"{url.rstrip('/')}/api/nokvo-one/agents/plivo/media/{link_id}?caller={caller}"
    rec = {"idx": idx, "connected": False, "first_audio_ms": None,
           "play_chunks": 0, "error": None}
    t_connect = time.monotonic()
    try:
        async with websockets.connect(media_url, max_size=None, open_timeout=15) as ws:
            rec["connected"] = True
            await ws.send(json.dumps({
                "event": "start",
                "streamId": f"load-{idx}",
                "start": {
                    "from": caller,
                    "mediaFormat": {"encoding": "audio/x-l16", "sampleRate": rate},
                },
            }))

            async def reader():
                async for msg in ws:
                    if not isinstance(msg, (str, bytes)):
                        continue
                    try:
                        ev = json.loads(msg).get("event")
                    except Exception:
                        continue
                    if ev == "playAudio":
                        rec["play_chunks"] += 1
                        if rec["first_audio_ms"] is None:
                            rec["first_audio_ms"] = round(
                                (time.monotonic() - t_connect) * 1000, 1)

            rtask = asyncio.create_task(reader())

            # Stream inbound audio at real-time pace.
            period = FRAME_MS / 1000
            next_t = time.monotonic()
            for fr in frames(pcm, rate):
                await ws.send(json.dumps({
                    "event": "media",
                    "media": {"track": "inbound", "payload": base64.b64encode(fr).decode()},
                }))
                next_t += period
                await asyncio.sleep(max(0, next_t - time.monotonic()))

            # Hold the line so the agent can finish its turn, then stop.
            await asyncio.sleep(hold)
            await ws.send(json.dumps({"event": "stop"}))
            await asyncio.sleep(0.2)
            rtask.cancel()
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
    results.append(rec)


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="wss base, e.g. wss://api.staging.example")
    p.add_argument("--link-id", required=True, help="tenant phone-link id")
    p.add_argument("--calls", type=int, default=10)
    p.add_argument("--ramp", type=float, default=2.0, help="seconds to stagger all connects over")
    p.add_argument("--audio", default=None, help="16-bit mono WAV to stream (optional)")
    p.add_argument("--rate", type=int, default=8000)
    p.add_argument("--seconds", type=float, default=4.0, help="tone length when no --audio")
    p.add_argument("--caller", default="+15555550100")
    p.add_argument("--hold", type=float, default=8.0, help="seconds to hold after audio")
    p.add_argument("--self-test", action="store_true", help="validate framing only; no network")
    args = p.parse_args()

    pcm = load_pcm(args.audio, args.rate, args.seconds)
    fr = list(frames(pcm, args.rate))
    print(f"audio: {len(pcm)} bytes -> {len(fr)} frames @ {FRAME_MS}ms ({args.rate}Hz)")
    if args.self_test:
        assert all(len(f) == int(args.rate * FRAME_MS / 1000) * 2 for f in fr)
        print("SELF-TEST OK")
        return 0

    results: list = []
    stagger = args.ramp / max(1, args.calls)
    tasks = []
    for i in range(args.calls):
        tasks.append(asyncio.create_task(
            run_call(i, args.url, args.link_id, args.caller, pcm, args.rate, args.hold, results)))
        await asyncio.sleep(stagger)
    await asyncio.gather(*tasks)

    connected = [r for r in results if r["connected"]]
    got_audio = [r for r in connected if r["first_audio_ms"] is not None]
    errors = [r for r in results if r["error"]]
    lat = sorted(r["first_audio_ms"] for r in got_audio)
    print("\n==== load summary ====")
    print(f"calls attempted : {args.calls}")
    print(f"connected       : {len(connected)}")
    print(f"got agent audio : {len(got_audio)}")
    print(f"errors          : {len(errors)}")
    if lat:
        print(f"first-audio ms  : p50={statistics.median(lat):.0f} "
              f"p95={lat[int(len(lat)*0.95)-1]:.0f} max={lat[-1]:.0f}")
    for r in errors[:5]:
        print(f"  err call#{r['idx']}: {r['error']}")
    # Non-zero exit if any call failed to get audio — useful as a CI gate.
    return 0 if got_audio and not errors else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
