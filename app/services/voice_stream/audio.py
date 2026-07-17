"""WAV/PCM helpers for the voice stream (probe + translate-STT framing).

Extracted from nokvo_one_voice_stream_service.py (which re-exports every
name here, so existing imports keep working). Byte-verbatim move — no
behavior change.
"""
from __future__ import annotations

import struct


def _extract_pcm_from_wav(wav: bytes) -> tuple[bytes, int] | None:
    """Return (pcm_data, sample_rate) for a RIFF/WAVE PCM16 mono blob, or
    ``None`` if the blob is not a parseable PCM16-mono WAV.

    Used by the audio-quality probe — Sarvam can also accept WebM/Opus
    but those need ffmpeg to decode, so we only score WAV blobs locally
    and let other formats pass through unscored.
    """
    if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        return None
    pos = 12
    fmt: tuple[int, int, int] | None = None
    data: bytes | None = None
    while pos + 8 <= len(wav):
        chunk_id = wav[pos : pos + 4]
        size = int.from_bytes(wav[pos + 4 : pos + 8], "little")
        body_start = pos + 8
        body_end = body_start + size
        if body_end > len(wav):
            break
        if chunk_id == b"fmt ":
            if size >= 16:
                audio_format = int.from_bytes(wav[body_start : body_start + 2], "little")
                channels = int.from_bytes(wav[body_start + 2 : body_start + 4], "little")
                sample_rate = int.from_bytes(wav[body_start + 4 : body_start + 8], "little")
                bps = int.from_bytes(wav[body_start + 14 : body_start + 16], "little")
                fmt = (audio_format, channels, sample_rate, bps)
        elif chunk_id == b"data":
            data = wav[body_start:body_end]
        pos = body_end + (size & 1)  # word-align
        if fmt is not None and data is not None:
            break
    if not fmt or data is None:
        return None
    audio_format, channels, sample_rate, bps = fmt
    if audio_format != 1 or channels != 1 or bps != 16:
        return None
    return data, sample_rate


def _pcm16le_to_wav(pcm: bytes, *, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw 16-bit little-endian PCM mono audio in a minimal WAV header.

    Used by the cross-lingual retrieval path: the translate-STT endpoint
    accepts a wav container; the streaming STT path captures raw PCM. This
    avoids pulling in a wave/ffmpeg dep just to add 44 bytes of header.
    """
    bits_per_sample = 16
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = len(pcm)
    riff_size = 36 + data_size
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", riff_size, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample,
        b"data", data_size,
    )
    return header + pcm
