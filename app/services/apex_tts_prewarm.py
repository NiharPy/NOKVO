"""Pre-warm the TTS byte-cache for an APEX deterministic campaign.

Every scripted line the campaign will speak — the admin-authored intro, each
questionnaire question, and the outro — recurs verbatim across calls, so its
audio is served from the Redis byte-cache (``SarvamVoiceService.synthesize``
``cache=True``). But the cache only fills on first use, so the FIRST call in
each language pays live synthesis for every line. This module synthesizes all
of them once, per language, right after campaign creation, so even call #1
plays cached audio.

Cache-key parity is the whole game: each line must be synthesized with the
SAME text/language/prosody args its call-time site uses, or the keys won't
match and the warm entry is dead weight:

  * questions  → one call per line with ``prosody_for("question", style)``
                 (mirrors ``_deliver_verbatim_question``);
  * outro      → one call with the style voice overlay (``style_prosody``) or
                 NO prosody args when the style has none — either way mirrors
                 ``_speak_outro_and_end``;
  * intro      → wrapped ``[warm]…[/warm]`` when untagged, split with
                 ``stream_prosody_chunks``, one call PER CHUNK with that
                 chunk's ``prosody_for(tone, style)`` (mirrors ``_play_opener``).

``style`` is the campaign's conversation style (``questionnaire["style"]``):
every call-time site composes its per-tone prosody with the style's voice
overlay, so the prewarm must too.

Raw text goes straight in — ``synthesize`` normalizes internally, and
normalizing here too would change the text and thus the key.

Best-effort throughout: fire-and-forget from the create endpoint, per-line
failures are swallowed, and nothing here can affect campaign creation.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from app.core.config import settings
from app.services.apex_micro_acks import _enabled_ack_languages, ack_pool
from app.services.prosody import prosody_for, stream_prosody_chunks, style_prosody
from app.services.sarvam_voice_service import SarvamVoiceService
from app.models.tenant_resources import TenantResources

logger = logging.getLogger(__name__)

# The languages the questionnaire is pre-translated into (questionnaire_translation).
_PREWARM_LANGS = ("en", "hi", "te")


async def _intro_lines(intro: str) -> list[tuple[str, str]]:
    """(text, tone) chunks for the opener, exactly as ``_play_opener`` splits
    them: untagged text is voiced warm; tagged text yields one chunk per tone
    segment, each synthesized separately with its own prosody."""
    text = (intro or "").strip()
    if not text:
        return []
    if "[" not in text or "]" not in text:
        text = f"[warm]{text}[/warm]"

    async def _single_chunk_stream() -> AsyncIterator[str]:
        yield text

    lines: list[tuple[str, str]] = []
    async for chunk in stream_prosody_chunks(_single_chunk_stream()):
        sentence = chunk.text.strip()
        if sentence:
            lines.append((sentence, chunk.tone))
    return lines


async def prewarm_campaign_tts(
    tenant_res: TenantResources, questionnaire: dict[str, Any] | None
) -> None:
    """Synthesize every scripted campaign line into the TTS byte-cache, for each
    pre-translated language AND each rendition variant (``APEX_TTS_VARIANTS``;
    each variant is a distinct cache key → a distinct natural take). Also warms
    the micro-ack pool for the campaign's conversation style
    (``APEX_ACK_ENABLED``) — pools are global per style×language, so after the
    first campaign of a style these are pure cache hits (synthesize probes the
    cache before calling Sarvam). Serial (one request at a time — this runs in
    the background, latency is irrelevant); never raises."""
    if not isinstance(questionnaire, dict):
        return
    warmed = failed = 0
    variants = max(1, int(settings.APEX_TTS_VARIANTS or 1))
    # The campaign's conversation style shapes the voice at every call-time
    # site (prosody style overlay) — compose it here identically or the keys
    # won't match. Empty/scripted/professional = no overlay = legacy keys.
    style = str(questionnaire.get("style") or "")
    _sp = style_prosody(style)
    # What _speak_outro_and_end passes: the style baseline, or nothing.
    close_kwargs: dict[str, Any] = (
        {"pace": _sp.pace, "pitch": _sp.pitch, "loudness": _sp.loudness} if _sp else {}
    )
    for lang in _PREWARM_LANGS:
        # (text, prosody kwargs) per line, mirroring each call-time site.
        jobs: list[tuple[str, dict[str, Any]]] = []
        question_prosody = prosody_for("question", style)
        for q in questionnaire.get("questions") or []:
            line = str((q.get("text_i18n") or {}).get(lang) or "").strip()
            if line:
                jobs.append(
                    (
                        line,
                        {
                            "pace": question_prosody.pace,
                            "pitch": question_prosody.pitch,
                            "loudness": question_prosody.loudness,
                        },
                    )
                )
        outro = str((questionnaire.get("outro_i18n") or {}).get(lang) or "").strip()
        if outro:
            jobs.append((outro, dict(close_kwargs)))
        # The BUSY dealbreaker close ("I'm busy, call me later" → this line +
        # hangup) — static per language, spoken via the same close path (which
        # applies the campaign's style overlay), so it warms with the same
        # kwargs. Global per style×language: after the first campaign of a
        # style these are cache probes.
        try:
            from app.services.nokvo_one_voice_stream_service import _busy_outro

            jobs.append((_busy_outro(lang), dict(close_kwargs)))
        except Exception:
            logger.debug("APEX-TTS-PREWARM: busy outro unavailable lang=%s", lang, exc_info=True)
        try:
            intro = str((questionnaire.get("intro_i18n") or {}).get(lang) or "")
            for sentence, tone in await _intro_lines(intro):
                prosody = prosody_for(tone, style)
                jobs.append(
                    (
                        sentence,
                        {"pace": prosody.pace, "pitch": prosody.pitch, "loudness": prosody.loudness},
                    )
                )
        except Exception:
            logger.debug("APEX-TTS-PREWARM: intro chunking failed lang=%s", lang, exc_info=True)
        # Micro-acks spoken before verbatim questions (warm tone at call time —
        # mirrors _deliver_verbatim_question's ack site). The pool matches the
        # campaign's conversation style; pools are global per style×lang, so
        # after the first campaign of a style these are pure cache probes.
        # Only warm the languages that will actually SPEAK an ack: choose_ack
        # gates on APEX_ACK_LANGUAGES (hi/te pools are drafts pending native-
        # speaker review), so warming them would synthesize — and pay for —
        # variants of lines no call can ever reach.
        if settings.APEX_ACK_ENABLED and lang in _enabled_ack_languages():
            ack_prosody = prosody_for("warm", style)
            for ack in ack_pool(questionnaire.get("style"), lang):  # pool langs == prewarm langs
                jobs.append(
                    (
                        ack,
                        {
                            "pace": ack_prosody.pace,
                            "pitch": ack_prosody.pitch,
                            "loudness": ack_prosody.loudness,
                        },
                    )
                )

        for line, prosody_kwargs in jobs:
            for variant in range(1, variants + 1):
                try:
                    await SarvamVoiceService.synthesize(
                        tenant_res,
                        line,
                        language=lang,
                        cache=True,
                        variant=variant,
                        **prosody_kwargs,
                    )
                    warmed += 1
                except Exception:
                    failed += 1
                    logger.debug(
                        "APEX-TTS-PREWARM: synthesis failed lang=%s variant=%d line=%r",
                        lang, variant, line[:60],
                        exc_info=True,
                    )
    if warmed or failed:
        logger.info("APEX-TTS-PREWARM: warmed %d line(s), %d failed", warmed, failed)
