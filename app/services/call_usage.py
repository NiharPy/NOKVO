"""Per-call usage accumulation + COGS (cost-of-goods-sold) breakdown.

A single voice call burns four metered vendor resources:

  * **STT**  — Sarvam speech-to-text, priced per audio-minute.
  * **LLM**  — Azure gpt-5-mini (shared pool), priced per 1K tokens (input /
    output / cached-input at distinct rates).
  * **TTS**  — Sarvam text-to-speech, priced per 1K characters.
  * **Plivo** — telephony, priced per call-minute.

The voice pipeline accumulates the raw counters into a :class:`CallUsage`
over the life of a session; at teardown the recorder converts them into the
per-component **INR** cost we persist on ``call_costs`` (the ``cost_*_inr``
columns). This is Nokvo's *cost*, distinct from ``call_costs.rupees`` which
is what the *tenant is billed* (the tiered post-paid tariff).

Why INR: the rest of the call ledger is rupee-denominated, so the SuperAdmin
console can show revenue (₹) − COGS (₹) = margin (₹) without mixing units.
The per-unit vendor rates live in ``settings`` as USD list prices; we convert
with ``settings.USD_TO_INR`` (hardcoded MVP FX — see the config comment).
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from app.core.config import settings


_Q = Decimal("0.0001")  # 4-dp ledger precision, matches call_cost_calculator


def _q(value: Decimal) -> Decimal:
    return value.quantize(_Q, rounding=ROUND_HALF_UP)


def _d(value) -> Decimal:
    """Coerce to Decimal via str so binary-float artefacts don't leak in."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


@dataclass
class CallUsage:
    """Mutable per-session tally of metered vendor usage.

    Instantiate once at session start, increment as turns happen, then hand
    to :func:`app.services.call_cost_recorder.record_call_cost`. All adders
    are null-safe so partial/short calls (or providers that don't report a
    figure) simply contribute zero.
    """

    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cached_tokens: int = 0
    stt_seconds: float = 0.0
    tts_characters: int = 0

    def add_llm(
        self,
        *,
        prompt_tokens=None,
        completion_tokens=None,
        cached_tokens=None,
    ) -> None:
        """Accumulate one LLM turn's usage chunk.

        ``prompt_tokens`` is the *total* input (cached + uncached) as Azure
        reports it; ``cached_tokens`` is the discounted-rate subset.
        """
        if prompt_tokens:
            self.llm_input_tokens += int(prompt_tokens)
        if completion_tokens:
            self.llm_output_tokens += int(completion_tokens)
        if cached_tokens:
            self.llm_cached_tokens += int(cached_tokens)

    def add_stt_seconds(self, seconds) -> None:
        if seconds and seconds > 0:
            self.stt_seconds += float(seconds)

    def add_tts_text(self, text: str | None) -> None:
        if text:
            self.tts_characters += len(text)

    def add_tts_chars(self, count) -> None:
        if count and count > 0:
            self.tts_characters += int(count)


# ── Ambient per-call usage sink ───────────────────────────────────────────
# The voice runtime is built from stateless static methods
# (SarvamVoiceService / NokvoOneVoicePipeline) called from deep inside a
# per-call ``run_session`` coroutine. Threading a CallUsage through every
# ``stream_sentence_tts`` / ``transcribe`` / LLM-stream call site would touch
# dozens of signatures. Instead each session installs its CallUsage in this
# contextvar at the top of run_session; the metered chokepoints fetch it with
# :func:`current_call_usage` and increment. contextvars are per-task and are
# copied into child tasks at ``create_task`` time, so as long as the session
# sets it before spawning turn/STT/TTS tasks, every increment lands on the one
# shared object. Concurrent calls each get their own sink (separate tasks).
_current_usage: contextvars.ContextVar[CallUsage | None] = contextvars.ContextVar(
    "nokvo_call_usage", default=None
)


def current_call_usage() -> CallUsage | None:
    """Return the CallUsage for the in-flight call, or None outside a session."""
    return _current_usage.get()


def begin_call_usage() -> tuple[CallUsage, contextvars.Token]:
    """Install a fresh CallUsage for this session. Returns it plus the reset
    token to pass to :func:`end_call_usage` in the session's finally block."""
    usage = CallUsage()
    token = _current_usage.set(usage)
    return usage, token


def end_call_usage(token: contextvars.Token) -> None:
    """Tear down the ambient sink (best-effort) at session end."""
    try:
        _current_usage.reset(token)
    except Exception:
        pass


@dataclass(frozen=True)
class CogsBreakdown:
    """Per-call vendor cost in INR, plus the raw counters that produced it."""

    cost_stt_inr: Decimal
    cost_llm_inr: Decimal
    cost_tts_inr: Decimal
    cost_telephony_inr: Decimal
    cost_total_inr: Decimal
    llm_input_tokens: int
    llm_output_tokens: int
    llm_cached_tokens: int
    stt_seconds: Decimal
    tts_characters: int


def compute_cogs_inr(usage: CallUsage, telephony_seconds) -> CogsBreakdown:
    """Price a call's accumulated usage into per-component INR COGS.

    ``telephony_seconds`` is the billable Plivo duration (the call's
    ``duration_seconds``) — telephony has no separate counter on
    :class:`CallUsage` because the call duration already is the meter.
    """
    fx = _d(settings.USD_TO_INR)

    # STT: per audio-minute.
    stt_usd = (_d(usage.stt_seconds) / Decimal("60")) * _d(settings.COST_PER_STT_MINUTE_USD)

    # LLM: input billed at the standard rate for the *uncached* portion and
    # the discounted rate for the cached portion; output at its own rate.
    cached = _d(usage.llm_cached_tokens)
    total_input = _d(usage.llm_input_tokens)
    uncached_input = total_input - cached
    if uncached_input < 0:
        uncached_input = Decimal("0")
    llm_usd = (
        (uncached_input / Decimal("1000")) * _d(settings.COST_PER_LLM_INPUT_1K_TOKENS_USD)
        + (cached / Decimal("1000")) * _d(settings.COST_PER_LLM_CACHED_INPUT_1K_TOKENS_USD)
        + (_d(usage.llm_output_tokens) / Decimal("1000")) * _d(settings.COST_PER_LLM_OUTPUT_1K_TOKENS_USD)
    )

    # TTS: per 1K characters synthesized.
    tts_usd = (_d(usage.tts_characters) / Decimal("1000")) * _d(settings.COST_PER_TTS_1K_CHARS_USD)

    # Telephony (Plivo): per call-minute.
    telephony_usd = (_d(telephony_seconds) / Decimal("60")) * _d(settings.COST_PER_TWILIO_MINUTE_USD)

    cost_stt = _q(stt_usd * fx)
    cost_llm = _q(llm_usd * fx)
    cost_tts = _q(tts_usd * fx)
    cost_telephony = _q(telephony_usd * fx)
    cost_total = _q(cost_stt + cost_llm + cost_tts + cost_telephony)

    return CogsBreakdown(
        cost_stt_inr=cost_stt,
        cost_llm_inr=cost_llm,
        cost_tts_inr=cost_tts,
        cost_telephony_inr=cost_telephony,
        cost_total_inr=cost_total,
        llm_input_tokens=int(usage.llm_input_tokens),
        llm_output_tokens=int(usage.llm_output_tokens),
        llm_cached_tokens=int(usage.llm_cached_tokens),
        stt_seconds=_q(_d(usage.stt_seconds)),
        tts_characters=int(usage.tts_characters),
    )
