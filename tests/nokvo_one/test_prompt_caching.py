"""Guards the prompt-caching restructure of compose_rag_messages.

The inbound prompt is split into a STATIC PREFIX (system msg #0, identical for a
given tenant+language every turn → provider prompt-caching hits) and a DYNAMIC
SUFFIX (system msg #1, the per-turn mode/memory/strategy). If any per-turn value
leaks into the prefix, caching silently breaks — this test is the tripwire.
"""
from __future__ import annotations

from app.services.pipeline.message_composer import compose_rag_messages


def _compose(**over):
    base = dict(
        query="hi",
        chunks=[],
        language="en",
        history=[],
        company_name="Acme Realty",
        single_prompt_guidance="Acme Realty sells premium flats in Hyderabad.",
        projects_block="Skyline Heights — Tukkuguda — 3/4 BHK — ₹1.2Cr",
        conversational_memory_block="# CONVERSATIONAL MEMORY\nName: (unknown)",
        conversation_strategy_block="# CONVERSATION STRATEGY\nturn 1",
        agent_mode_block="# AGENT MODE — LEAD_CAPTURE",
        field_questions_prompt=None,
    )
    base.update(over)
    return compose_rag_messages(**base)


def test_static_prefix_is_identical_across_turns():
    # Turn 1 vs a later turn: only per-turn inputs differ.
    t1 = _compose(
        query="I want a 3 BHK", history=[],
        conversational_memory_block="# CONVERSATIONAL MEMORY\nName: (unknown)",
        conversation_strategy_block="# CONVERSATION STRATEGY\nturn 1",
        agent_mode_block="# AGENT MODE — LEAD_CAPTURE",
    )
    t2 = _compose(
        query="and the price?",
        history=[{"role": "user", "content": "I want a 3 BHK"},
                 {"role": "assistant", "content": "Sure!"}],
        conversational_memory_block="# CONVERSATIONAL MEMORY\nName: Nihar\nLooking for: 3 BHK",
        conversation_strategy_block="# CONVERSATION STRATEGY\nturn 2 — answer price",
        agent_mode_block="# AGENT MODE — BOOKING",
    )
    assert t1[0]["role"] == "system" and t2[0]["role"] == "system"
    # The cache-able prefix MUST be byte-identical across turns.
    assert t1[0]["content"] == t2[0]["content"], "static prefix drifted between turns — caching will miss"


def test_dynamic_content_is_in_the_suffix_not_the_prefix():
    msgs = _compose()
    prefix = msgs[0]["content"]
    suffix = msgs[1]["content"]  # second system message
    # Per-turn blocks must NOT be in the prefix.
    assert "AGENT MODE" not in prefix and "CONVERSATIONAL MEMORY" not in prefix
    assert "CONVERSATION STRATEGY" not in prefix
    # …and they MUST be in the dynamic suffix.
    assert "AGENT MODE" in suffix and "CONVERSATIONAL MEMORY" in suffix and "CONVERSATION STRATEGY" in suffix
    # Static brand/inventory/rules live in the prefix.
    assert "Skyline Heights" in prefix and "GROUNDING" in prefix and "PROSODY" in prefix


def test_prefix_is_meaningfully_trimmed():
    # The whole point: a single tenant prefix should be well under the old ~4.8K
    # tokens. Rough proxy: characters (≈4/token) — expect < ~10k chars (~2.5k tok).
    prefix = _compose()[0]["content"]
    assert len(prefix) < 10000, f"prefix too large ({len(prefix)} chars) — trim regressed"


def test_no_dead_rag_context_slot():
    # The retired KB left a 'Retrieved tenant context' slot — must be gone.
    msgs = _compose()
    user_msg = msgs[-1]["content"]
    assert "Retrieved tenant context" not in user_msg
