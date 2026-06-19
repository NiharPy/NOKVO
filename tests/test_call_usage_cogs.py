"""Per-call COGS math — CallUsage accumulation + compute_cogs_inr.

DB-free: exercises the pricing logic the call-cost recorder persists into the
``cost_*_inr`` columns. Expected values are derived from ``settings`` so the
test tracks rate/FX changes instead of hardcoding a frozen number.
"""
from decimal import Decimal

from app.core.config import settings
from app.services.call_usage import CallUsage, compute_cogs_inr


def _expected_inr(usd: Decimal) -> Decimal:
    return (usd * Decimal(str(settings.USD_TO_INR))).quantize(Decimal("0.0001"))


def test_zero_usage_is_all_zero():
    cogs = compute_cogs_inr(CallUsage(), telephony_seconds=0)
    assert cogs.cost_stt_inr == Decimal("0.0000")
    assert cogs.cost_llm_inr == Decimal("0.0000")
    assert cogs.cost_tts_inr == Decimal("0.0000")
    assert cogs.cost_telephony_inr == Decimal("0.0000")
    assert cogs.cost_total_inr == Decimal("0.0000")


def test_total_is_sum_of_components():
    usage = CallUsage(
        llm_input_tokens=1200, llm_output_tokens=600, llm_cached_tokens=200,
        stt_seconds=90, tts_characters=850,
    )
    cogs = compute_cogs_inr(usage, telephony_seconds=90)
    assert cogs.cost_total_inr == (
        cogs.cost_stt_inr + cogs.cost_llm_inr + cogs.cost_tts_inr + cogs.cost_telephony_inr
    )
    # Every component is positive for positive inputs.
    for value in (cogs.cost_stt_inr, cogs.cost_llm_inr, cogs.cost_tts_inr, cogs.cost_telephony_inr):
        assert value > 0


def test_component_values_match_rates():
    usage = CallUsage(
        llm_input_tokens=1000, llm_output_tokens=500, llm_cached_tokens=200,
        stt_seconds=120, tts_characters=600,
    )
    cogs = compute_cogs_inr(usage, telephony_seconds=120)

    stt_usd = (Decimal("120") / 60) * Decimal(str(settings.COST_PER_STT_MINUTE_USD))
    tts_usd = (Decimal("600") / 1000) * Decimal(str(settings.COST_PER_TTS_1K_CHARS_USD))
    tel_usd = (Decimal("120") / 60) * Decimal(str(settings.COST_PER_TWILIO_MINUTE_USD))
    # 800 uncached input + 200 cached + 500 output.
    llm_usd = (
        (Decimal("800") / 1000) * Decimal(str(settings.COST_PER_LLM_INPUT_1K_TOKENS_USD))
        + (Decimal("200") / 1000) * Decimal(str(settings.COST_PER_LLM_CACHED_INPUT_1K_TOKENS_USD))
        + (Decimal("500") / 1000) * Decimal(str(settings.COST_PER_LLM_OUTPUT_1K_TOKENS_USD))
    )

    assert cogs.cost_stt_inr == _expected_inr(stt_usd)
    assert cogs.cost_tts_inr == _expected_inr(tts_usd)
    assert cogs.cost_telephony_inr == _expected_inr(tel_usd)
    assert cogs.cost_llm_inr == _expected_inr(llm_usd)


def test_cached_exceeding_prompt_does_not_go_negative():
    # Defensive: cached should be a subset of prompt, but a provider quirk
    # reporting cached > prompt must not produce a negative LLM cost.
    usage = CallUsage(llm_input_tokens=100, llm_output_tokens=0, llm_cached_tokens=500)
    cogs = compute_cogs_inr(usage, telephony_seconds=0)
    assert cogs.cost_llm_inr >= 0


def test_callusage_adders_are_null_safe_and_accumulate():
    usage = CallUsage()
    usage.add_llm(prompt_tokens=None, completion_tokens=None, cached_tokens=None)  # no-op
    usage.add_llm(prompt_tokens=100, completion_tokens=50, cached_tokens=10)
    usage.add_llm(prompt_tokens=200, completion_tokens=20)
    usage.add_stt_seconds(0)       # no-op
    usage.add_stt_seconds(12.5)
    usage.add_tts_text(None)       # no-op
    usage.add_tts_text("hello")    # +5
    usage.add_tts_chars(15)

    assert usage.llm_input_tokens == 300
    assert usage.llm_output_tokens == 70
    assert usage.llm_cached_tokens == 10
    assert usage.stt_seconds == 12.5
    assert usage.tts_characters == 20


def test_cogs_carries_raw_counters():
    usage = CallUsage(llm_input_tokens=10, llm_output_tokens=5, llm_cached_tokens=2, stt_seconds=3, tts_characters=7)
    cogs = compute_cogs_inr(usage, telephony_seconds=3)
    assert cogs.llm_input_tokens == 10
    assert cogs.llm_output_tokens == 5
    assert cogs.llm_cached_tokens == 2
    assert cogs.stt_seconds == Decimal("3.0000")
    assert cogs.tts_characters == 7
