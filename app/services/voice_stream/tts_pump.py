"""Background TTS dispatcher (_TtsPump) and the pace/style knobs it applies.

Extracted from nokvo_one_voice_stream_service.py (which re-exports every
name here, so existing imports keep working). Byte-verbatim move — no
behavior change.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket

from app.models.tenant_resources import TenantResources
from app.services.prosody import DEFAULT_TONE, prosody_for, style_prosody
from app.services.sarvam_voice_service import SarvamVoiceService


# How many sentences may be combined into a single TTS call after the first
# one has been spoken. The first sentence is ALWAYS dispatched on its own so
# first-audio latency stays minimal; sentences that arrive while the worker
# is busy synthesising are coalesced up to this size to amortise the Sarvam
# REST roundtrip across multiple sentences.
_TTS_BATCH_MAX = 2


def _campaign_voice_style(outbound_context: Any) -> str:
    """The campaign's selected conversation style (``questionnaire_style``) —
    feeds the style voice overlay (``prosody.style_prosody``) at every scripted
    and LLM TTS site, so the selected style shapes the agent's pitch/pace, not
    just its wording. Empty for inbound calls and unstyled campaigns (identity
    overlay: those paths stay byte-identical, warmed cache keys included)."""
    return str(getattr(outbound_context, "questionnaire_style", "") or "")


def _scaled_pace(base_pace: float | None, factor: float) -> float | None:
    """Scale a TTS pace by ``factor``, clamped to Sarvam's 0.3–3.0 range.

    A ``factor`` of 1.0 returns ``base_pace`` unchanged (``None`` stays ``None``)
    so non-scaled paths are byte-identical. When scaling and ``base_pace`` is
    ``None`` (no explicit per-tone pace), the neutral 1.0 baseline is used so the
    factor still applies (it then composes with the per-language pace factor in
    ``stream_sentence_tts``)."""
    if factor == 1.0:
        return base_pace
    base = 1.0 if base_pace is None else float(base_pace)
    return max(0.3, min(3.0, base * factor))


class _TtsPump:
    """Background TTS dispatcher that batches sentences after the first one.

    Calling ``submit(sentence, tone)`` is non-blocking — the sentence is
    enqueued and the LLM stream loop continues to read the next token
    without waiting for TTS network latency. A single worker drains the
    queue in order, firing the first sentence as soon as it lands and then
    coalescing any sentences that piled up while TTS was in flight into a
    single batched Sarvam call (up to :data:`_TTS_BATCH_MAX`).

    Ordering is preserved end-to-end: the worker awaits each TTS call
    before pulling the next, so the audio packets emitted on the websocket
    arrive in sentence order.
    """

    def __init__(
        self,
        *,
        websocket: WebSocket,
        tenant_res: TenantResources,
        language: str,
        turn_id: str,
        purpose: str = "answer",
        speaking_mark: Any | None = None,
        pace_factor: float = 1.0,
        style: str = "",
    ) -> None:
        self._websocket = websocket
        self._tenant_res = tenant_res
        self._language = language
        self._turn_id = turn_id
        self._purpose = purpose
        self._speaking_mark = speaking_mark
        # Multiplier applied to every sentence's pace (1.0 = unchanged). Used to
        # slow outbound delivery slightly; composes with the per-language pace
        # factor inside stream_sentence_tts.
        self._pace_factor = pace_factor
        # Campaign conversation style (questionnaire_style): composes the style
        # voice overlay into every sentence's prosody so LLM off-script turns
        # sound like the scripted lines. Empty = no overlay (unchanged).
        self._style = style
        self._queue: asyncio.Queue[tuple[str, str, bool] | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._first_audio_fired = False

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def submit(self, sentence: str, tone: str, *, cacheable_tts: bool = False) -> None:
        if not sentence:
            return
        await self._queue.put((sentence, tone, cacheable_tts))

    async def close(self) -> None:
        """Send the end-of-stream sentinel and wait for the worker to flush
        all buffered sentences. Safe to call multiple times."""
        if self._worker is None:
            return
        await self._queue.put(None)
        try:
            await self._worker
        except asyncio.CancelledError:
            raise
        finally:
            self._worker = None

    async def cancel(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except (asyncio.CancelledError, Exception):
            pass
        self._worker = None

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            batch: list[tuple[str, str, bool]] = [item]
            # First sentence: dispatch alone so first audio lands as fast
            # as possible. After that, opportunistically drain any extra
            # sentences that piled up while the previous TTS call was in
            # flight — they collapse into one Sarvam round trip.
            if self._first_audio_fired:
                while len(batch) < _TTS_BATCH_MAX:
                    try:
                        more = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if more is None:
                        await self._flush(batch)
                        return
                    batch.append(more)
            await self._flush(batch)
            self._first_audio_fired = True

    async def _flush(self, batch: list[tuple[str, str, bool]]) -> None:
        if not batch:
            return
        text = " ".join(s for s, _, _ in batch).strip()
        if not text:
            return
        # Use the tone of the first sentence in the batch for prosody —
        # adjacent sentences from the same LLM completion almost always
        # carry the same emotional register. The first sentence of a turn
        # skips tone prosody (fast path) but still carries the campaign's
        # style overlay, so a styled agent's voice doesn't flip register
        # between its first and second sentence (None when unstyled — the
        # inbound/unstyled path stays byte-identical).
        prosody = (
            style_prosody(self._style)
            if not self._first_audio_fired
            else prosody_for(batch[0][1] or DEFAULT_TONE, self._style)
        )
        if self._speaking_mark is not None:
            try:
                self._speaking_mark()
            except Exception:
                pass
        # Apply the pace multiplier even on the first sentence (prosody=None →
        # baseline 1.0). Factor 1.0 leaves pace untouched (None stays None) so
        # the inbound path is byte-identical.
        base_pace = prosody.pace if prosody else None
        pace = _scaled_pace(base_pace, self._pace_factor)
        try:
            await SarvamVoiceService.stream_sentence_tts(
                self._websocket,
                self._tenant_res,
                text,
                language=self._language,
                purpose=self._purpose,
                pace=pace,
                pitch=prosody.pitch if prosody else None,
                loudness=prosody.loudness if prosody else None,
                enable_cached_responses=all(cacheable for _, _, cacheable in batch),
            )
        except Exception as exc:
            try:
                await self._websocket.send_json(
                    {
                        "type": "tts_error",
                        "turn_id": self._turn_id,
                        "error_message": str(exc)[:240],
                        "provider": "sarvam",
                    }
                )
            except Exception:
                pass
