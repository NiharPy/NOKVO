"""Conversation-strategy unit tests.

Locks the tactical layer the inbound agent gained on top of conversational
memory:

  * lead scoring picks the right tier from the signals already in memory;
  * the strategy block stays empty on a cold-open turn (no TTFT cost);
  * an active objection injects its playbook;
  * a known income with no budget unlocks affordability reasoning;
  * a frustrated / re-asked caller triggers recovery (universal — any domain);
  * the sales layer is suppressed for non-sales business types;
  * a None business type never raises;
  * the rendered block respects its character cap.

Pure surface only — no Redis, no LLM.
"""
from __future__ import annotations

from app.services.conversation_strategy import (
    OBJECTION_PLAYBOOK,
    compose_strategy_block,
    journey_stage,
    score_lead,
)
from app.services.conversational_memory import ConversationalMemory


def _re(text: str, mem: ConversationalMemory | None = None, turn: int = 1) -> ConversationalMemory:
    mem = mem or ConversationalMemory()
    mem.merge_text(text, turn_index=turn, business_type="real_estate")
    return mem


# ── Lead scoring ─────────────────────────────────────────────────────────────


def test_score_lead_hot_on_visit_and_preapproval() -> None:
    m = _re("I want to visit this Saturday, I have pre-approval")
    assert score_lead(m, business_type="real_estate").tier == "hot"


def test_score_lead_hot_on_ready_to_book() -> None:
    m = _re("let's go ahead and book it")
    assert score_lead(m, business_type="real_estate").tier == "hot"


def test_score_lead_cold_on_long_horizon() -> None:
    m = _re("maybe sometime next year, just exploring")
    assert score_lead(m, business_type="real_estate").tier == "cold"


def test_score_lead_warm_on_partial_qualification() -> None:
    m = _re("looking for a 3 BHK in Kompally")
    assert score_lead(m, business_type="real_estate").tier == "warm"


def test_score_lead_at_risk_on_repetition() -> None:
    m = _re("you keep asking me the same thing")
    assert score_lead(m, business_type="real_estate").tier == "at_risk"


def test_score_lead_do_not_call_is_cold() -> None:
    m = _re("not interested, please remove me from your list")
    assert score_lead(m, business_type="real_estate").tier == "cold"


# ── Journey stage ────────────────────────────────────────────────────────────


def test_journey_stage_discovery_when_empty() -> None:
    assert journey_stage(ConversationalMemory(), business_type="real_estate") == "discovery"


def test_journey_stage_objection_handling() -> None:
    m = _re("this is too expensive")
    assert journey_stage(m, business_type="real_estate") == "objection_handling"


def test_journey_stage_closing_on_ready_to_book() -> None:
    m = _re("okay please book it")
    assert journey_stage(m, business_type="real_estate") == "closing"


# ── Strategy block assembly ──────────────────────────────────────────────────


def test_block_empty_on_cold_open() -> None:
    """A brand-new memory has no actionable signal — the block must be empty so
    cold-open time-to-first-token is unaffected."""
    assert compose_strategy_block(ConversationalMemory(), business_type="real_estate") == ""


def test_block_includes_price_playbook() -> None:
    m = _re("honestly that's too expensive for me")
    block = compose_strategy_block(m, business_type="real_estate")
    assert OBJECTION_PLAYBOOK["price_concern"] in block


def test_block_affordability_when_income_no_budget() -> None:
    m = _re("what can I afford? I earn about 1.5 lakhs a month")
    block = compose_strategy_block(m, business_type="real_estate")
    assert "AFFORDABILITY" in block


def test_block_no_affordability_when_budget_known() -> None:
    m = ConversationalMemory()
    m.merge_text("my budget is 90 lakhs", turn_index=1, business_type="real_estate")
    m.merge_text("I earn 2 lakhs a month", turn_index=2, business_type="real_estate")
    block = compose_strategy_block(m, business_type="real_estate")
    assert "AFFORDABILITY" not in block


def test_block_recovery_on_repetition_is_universal() -> None:
    """Recovery fires for ANY business type, not just sales."""
    m = ConversationalMemory()
    m.merge_text("you already asked me that", turn_index=1, business_type="clinics")
    block = compose_strategy_block(m, business_type="clinics")
    assert "RECOVER THE CALL" in block


def test_block_sales_layer_suppressed_for_non_sales() -> None:
    """A clinic mentioning 'expensive' is not a real-estate price objection —
    no lead strategy / playbook should appear."""
    m = ConversationalMemory()
    m.merge_text("the consultation is too expensive", turn_index=1, business_type="clinics")
    block = compose_strategy_block(m, business_type="clinics")
    assert "LEAD STRATEGY" not in block
    assert OBJECTION_PLAYBOOK["price_concern"] not in block


def test_block_none_business_type_does_not_raise() -> None:
    m = _re("looking for a 2 BHK")
    # No sales layer without a sales business type, and must not raise.
    block = compose_strategy_block(m, business_type=None)
    assert "LEAD STRATEGY" not in block
    assert isinstance(block, str)


def test_block_none_business_type_still_recovers() -> None:
    m = ConversationalMemory()
    m.merge_text("stop asking me the same thing", turn_index=1)
    block = compose_strategy_block(m, business_type=None)
    assert "RECOVER THE CALL" in block


def test_block_outbound_enables_sales_layer() -> None:
    m = _re("that's too expensive")
    block = compose_strategy_block(m, business_type=None, is_outbound=True)
    assert "LEAD STRATEGY" in block


def test_block_respects_char_cap_with_all_signals() -> None:
    m = ConversationalMemory()
    m.merge_text("you keep asking me the same thing", turn_index=1, business_type="real_estate")
    m.merge_text(
        "that's too expensive and I already have another broker",
        turn_index=2,
        business_type="real_estate",
    )
    m.merge_text("I earn 2 lakhs a month", turn_index=3, business_type="real_estate")
    m.prior_stage = "closing"
    block = compose_strategy_block(m, business_type="real_estate")
    assert 0 < len(block) <= 1800
