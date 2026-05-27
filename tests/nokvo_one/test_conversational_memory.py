"""Conversational-memory unit tests.

Locks the behaviour the live voice agent depends on:

  * The extractor catches the slot patterns we care about (English +
    code-switched Hindi / Telugu / Tamil).
  * A higher-confidence correction overrides an earlier fact of the
    same key, while equal-confidence updates respect recency.
  * The "don't re-ask" prompt block lists every known fact.
  * The flow-collected hydrator pre-fills slots from memory so
    tool_flow's ``_next_slot`` skips them.

We deliberately exercise the public surface only — no patching of
Redis or the LLM. The store layer is exercised in
:mod:`tests.nokvo_one.test_runtime_bundle` and integration tests.
"""
from __future__ import annotations

import pytest

from app.services.conversational_memory import (
    BUCKET_OBJECTIONS,
    ConversationalMemory,
    FACT_BHK,
    FACT_BUDGET,
    FACT_EMAIL,
    FACT_LOCATION,
    FACT_NAME,
    FACT_PHONE,
    FACT_VISIT_DATE,
    MemoryExtractor,
    MemoryFact,
    hydrate_flow_collected,
)


def test_extractor_english_name_phone_bhk_budget_location() -> None:
    """One canonical English utterance covers the four core slots."""
    m = ConversationalMemory()
    m.merge_text(
        "Hi, my name is Asha. Looking for 3BHK in Kompally around 80 lakhs",
        turn_index=1,
        language="en",
    )
    snap = m.snapshot()
    assert snap[FACT_NAME] == "Asha"
    assert snap[FACT_BHK] == "3 BHK"
    assert "80 lakhs" in snap[FACT_BUDGET]
    assert snap[FACT_LOCATION] == "Kompally"


def test_extractor_phone_with_country_code_normalises_to_ten_digits() -> None:
    m = ConversationalMemory()
    m.merge_text("My number is +91 98765 43210", turn_index=1)
    assert m.snapshot()[FACT_PHONE] == "9876543210"


def test_extractor_email() -> None:
    m = ConversationalMemory()
    m.merge_text("Send the brochure to asha.r@example.co.in please", turn_index=1)
    assert m.snapshot()[FACT_EMAIL] == "asha.r@example.co.in"


def test_extractor_hindi_transliterated_name_strips_copula() -> None:
    """``mera naam Rahul hai`` must yield ``Rahul``, not ``Rahul hai``."""
    m = ConversationalMemory()
    m.merge_text("mera naam Rahul hai, 4 BHK chahiye", turn_index=1, language="hi")
    snap = m.snapshot()
    assert snap[FACT_NAME] == "Rahul"
    assert snap[FACT_BHK] == "4 BHK"


def test_extractor_telugu_name_strips_honorific() -> None:
    m = ConversationalMemory()
    m.merge_text("naa peru Asha andi", turn_index=1, language="te")
    assert m.snapshot()[FACT_NAME] == "Asha"


def test_extractor_visit_date() -> None:
    m = ConversationalMemory()
    m.merge_text("Can I come tomorrow afternoon?", turn_index=1)
    assert m.snapshot()[FACT_VISIT_DATE] == "tomorrow"


def test_correction_cue_overrides_earlier_fact() -> None:
    """``no actually 4BHK`` must rewrite an earlier ``3BHK``."""
    m = ConversationalMemory()
    m.merge_text("I want 3 BHK", turn_index=1)
    m.merge_text("no actually 4 BHK", turn_index=2)
    assert m.snapshot()[FACT_BHK] == "4 BHK"


def test_equal_confidence_newer_turn_wins() -> None:
    """Two non-correction caller utterances with the same confidence:
    the later one wins so a caller updating themselves naturally is
    still respected."""
    m = ConversationalMemory()
    m.merge_text("looking at 3 BHK", turn_index=1)
    m.merge_text("ok let's go with 4 BHK instead", turn_index=2)
    assert m.snapshot()[FACT_BHK] == "4 BHK"


def test_objection_bucket_captures_not_interested() -> None:
    m = ConversationalMemory()
    m.merge_text("not interested, please remove me", turn_index=1)
    latest = m.latest_objection()
    assert latest is not None
    assert latest["code"] == "do_not_call"


def test_prompt_block_includes_every_known_fact() -> None:
    m = ConversationalMemory()
    m.merge_text(
        "my name is Asha, 3 BHK around 80 lakhs in Kompally",
        turn_index=1,
    )
    block = m.compose_prompt_block(language="en")
    assert "CONVERSATIONAL MEMORY" in block
    assert "do NOT ask" in block.lower() or "do not ask" in block.lower()
    assert "Asha" in block
    assert "3 BHK" in block
    assert "Kompally" in block


def test_prompt_block_empty_when_nothing_known() -> None:
    m = ConversationalMemory()
    assert m.compose_prompt_block() == ""


def test_hydrate_flow_collected_fills_known_slots_only() -> None:
    """``hydrate_flow_collected`` pre-populates the booking flow's
    ``collected`` dict from memory but never overwrites explicit
    flow writes."""
    m = ConversationalMemory()
    m.merge_text(
        "Hi, my name is Asha, 3 BHK around 80 lakhs",
        turn_index=1,
    )
    pre_collected = {"customer_name": "Should-not-overwrite"}
    out = hydrate_flow_collected(pre_collected, m)
    # Existing entry preserved.
    assert out["customer_name"] == "Should-not-overwrite"
    # Memory-supplied gap filled.
    assert out.get("bhk") == "3 BHK"
    assert out.get("budget", "").endswith("lakhs")


def test_memory_state_blob_roundtrip() -> None:
    m = ConversationalMemory()
    m.merge_text("my name is Asha, 3BHK in Kompally", turn_index=1)
    m.merge_text("not interested in 2BHK", turn_index=2)
    blob = m.to_state_blob()
    restored = ConversationalMemory.from_state_blob(blob)
    assert restored.snapshot()[FACT_NAME] == m.snapshot()[FACT_NAME]
    assert restored.snapshot()[FACT_BHK] == m.snapshot()[FACT_BHK]
    assert len(restored.objections) == len(m.objections)


def test_assistant_confirmation_locks_fact_at_lower_confidence() -> None:
    """An agent echoing a slot should set memory but at a confidence
    lower than the user's own turn so a later user correction wins."""
    m = ConversationalMemory()
    m.merge_text("Got it, 3 BHK in Kompally.", turn_index=1, role="assistant")
    assert m.snapshot()[FACT_BHK] == "3 BHK"
    # User now corrects (no correction-cue, just an equal-confidence
    # update — but the assistant message used lower confidence so a
    # plain user follow-up still wins).
    m.merge_text("Actually 4 BHK works better", turn_index=2, role="user")
    assert m.snapshot()[FACT_BHK] == "4 BHK"


def test_extractor_skips_empty_or_whitespace() -> None:
    m = ConversationalMemory()
    m.merge_text("", turn_index=1)
    m.merge_text("   ", turn_index=2)
    assert m.snapshot() == {}


def test_has_asked_recently_within_window() -> None:
    m = ConversationalMemory()
    m.mark_asked("budget", 1)
    m.mark_asked("name", 3)
    assert m.has_asked_recently("budget", within_turns=4)
    assert m.has_asked_recently("name", within_turns=1)


def test_memory_fact_dict_roundtrip_preserves_confidence() -> None:
    fact = MemoryFact(
        key=FACT_NAME,
        value="Asha",
        confidence=0.85,
        source_turn=2,
        timestamp=123456.0,
        language="en",
        raw="my name is asha",
    )
    restored = MemoryFact.from_dict(fact.to_dict())
    assert restored.value == "Asha"
    assert restored.confidence == 0.85
    assert restored.source_turn == 2
    assert restored.language == "en"
