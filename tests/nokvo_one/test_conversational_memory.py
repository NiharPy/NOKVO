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
    FACT_APPOINTMENT_TYPE,
    FACT_BHK,
    FACT_BUDGET,
    FACT_CHECK_IN,
    FACT_CHECK_OUT,
    FACT_DIETARY,
    FACT_DOCTOR_PREFERENCE,
    FACT_EMAIL,
    FACT_ISSUE_TYPE,
    FACT_ITEM,
    FACT_LOCATION,
    FACT_NAME,
    FACT_OCCASION,
    FACT_ORDER_ID,
    FACT_PARTY_SIZE,
    FACT_PATIENT_AGE,
    FACT_PHONE,
    FACT_PROPERTY,
    FACT_ROOM_TYPE,
    FACT_SYMPTOMS,
    FACT_TRACKING_NUMBER,
    FACT_VISIT_DATE,
    FLOW_SLOT_TO_FACT,
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


# ── Per-business-type extraction ─────────────────────────────────────────────


def test_clinics_extracts_symptoms_doctor_age() -> None:
    m = ConversationalMemory()
    m.merge_text(
        "I have fever and a cough, want a follow-up with Dr. Sharma, my son is 7 years old",
        turn_index=1,
        business_type="clinics",
    )
    snap = m.snapshot()
    assert "fever" in snap[FACT_SYMPTOMS]
    assert snap[FACT_APPOINTMENT_TYPE] == "follow-up"
    assert snap[FACT_DOCTOR_PREFERENCE] == "Dr. Sharma"
    assert snap[FACT_PATIENT_AGE] == "7"


def test_clinics_does_not_extract_real_estate_slots() -> None:
    """A clinic call must not mine BHK/budget even if the words appear."""
    m = ConversationalMemory()
    m.merge_text(
        "I want 3 BHK worth of medicine around 80 lakhs of patients",
        turn_index=1,
        business_type="clinics",
    )
    assert FACT_BHK not in m.snapshot()
    assert FACT_BUDGET not in m.snapshot()


def test_ecommerce_extracts_order_issue_item() -> None:
    m = ConversationalMemory()
    m.merge_text(
        "My order number is ORD-12345, I ordered a blue kettle but it is damaged",
        turn_index=1,
        business_type="ecommerce",
    )
    snap = m.snapshot()
    assert snap[FACT_ORDER_ID] == "ORD-12345"
    assert snap[FACT_ISSUE_TYPE] == "damaged"
    assert snap[FACT_ITEM] == "blue kettle"


def test_ecommerce_tracking_number() -> None:
    m = ConversationalMemory()
    m.merge_text(
        "tracking number AWB9988776 hasn't moved in days",
        turn_index=1,
        business_type="ecommerce",
    )
    assert m.snapshot()[FACT_TRACKING_NUMBER] == "AWB9988776"


def test_hospitality_extracts_party_dates_occasion() -> None:
    m = ConversationalMemory()
    m.merge_text(
        "Table for four, it's our anniversary and we are vegetarian",
        turn_index=1,
        business_type="hospitality",
    )
    snap = m.snapshot()
    assert snap[FACT_PARTY_SIZE] == "4"
    assert snap[FACT_OCCASION] == "anniversary"
    assert snap[FACT_DIETARY] == "vegetarian"


def test_hospitality_check_in_check_out() -> None:
    m = ConversationalMemory()
    m.merge_text(
        "I'd like a deluxe room, check in on Friday and check out Sunday",
        turn_index=1,
        business_type="hospitality",
    )
    snap = m.snapshot()
    assert snap[FACT_ROOM_TYPE] == "deluxe room"
    assert "Friday" in snap[FACT_CHECK_IN]
    assert "Sunday" in snap[FACT_CHECK_OUT]


def test_unknown_business_type_runs_superset() -> None:
    """``other`` / unknown business types capture across every domain so
    nothing is silently dropped for an unclassified business."""
    m = ConversationalMemory()
    m.merge_text(
        "My name is Ravi, order #A1B2C3 is broken",
        turn_index=1,
        business_type="other",
    )
    snap = m.snapshot()
    assert snap[FACT_NAME] == "Ravi"
    assert snap[FACT_ORDER_ID] == "A1B2C3"
    assert snap[FACT_ISSUE_TYPE] == "damaged"


def test_prompt_block_filters_by_business_type() -> None:
    """Clinic prompt block omits real-estate slot lines and vice-versa."""
    m = ConversationalMemory()
    m.merge_text("my name is Asha, 3 BHK in Kompally", turn_index=1, business_type="real_estate")
    # Cross-render the same memory under a clinic lens — BHK/location must
    # not appear because they aren't clinic prompt keys.
    clinic_block = m.compose_prompt_block(business_type="clinics")
    assert "BHK preference" not in clinic_block
    assert "Location" not in clinic_block
    # Real-estate lens shows them.
    re_block = m.compose_prompt_block(business_type="real_estate")
    assert "3 BHK" in re_block
    assert "Kompally" in re_block


# ── In-conversation salient recall ───────────────────────────────────────────


def test_salient_note_captures_allergy() -> None:
    m = ConversationalMemory()
    m.merge_text("By the way I'm allergic to penicillin", turn_index=1, business_type="clinics")
    texts = [n.get("text") for n in m.salient_notes]
    assert any("allergic to penicillin" in t for t in texts)


def test_salient_notes_dedupe_repeated_statement() -> None:
    m = ConversationalMemory()
    m.merge_text("I'm allergic to peanuts", turn_index=1)
    m.merge_text("Remember, I'm allergic to peanuts", turn_index=3)
    allergy_notes = [n for n in m.salient_notes if "peanut" in str(n.get("text"))]
    assert len(allergy_notes) == 1


def test_salient_only_from_caller_not_agent() -> None:
    m = ConversationalMemory()
    m.merge_text("Please remember to bring your ID card", turn_index=1, role="assistant")
    assert m.salient_notes == []


def test_salient_renders_in_prompt_block() -> None:
    m = ConversationalMemory()
    m.merge_text("I'm allergic to shellfish", turn_index=1, business_type="hospitality")
    block = m.compose_prompt_block(business_type="hospitality")
    assert "Key details to remember" in block
    assert "shellfish" in block


def test_salient_notes_survive_state_blob_roundtrip() -> None:
    m = ConversationalMemory()
    m.merge_text("I'm allergic to dust", turn_index=1)
    restored = ConversationalMemory.from_state_blob(m.to_state_blob())
    assert any("dust" in str(n.get("text")) for n in restored.salient_notes)


# ── Cross-call durable subset selection ──────────────────────────────────────


def test_durable_fact_keys_are_business_specific() -> None:
    from app.services.conversational_memory import _durable_fact_keys_for

    clinic_keys = _durable_fact_keys_for("clinics")
    assert FACT_DOCTOR_PREFERENCE in clinic_keys
    assert FACT_BHK not in clinic_keys
    re_keys = _durable_fact_keys_for("real_estate")
    assert FACT_BHK in re_keys
    assert FACT_DOCTOR_PREFERENCE not in re_keys
    # Unknown → superset includes both.
    other_keys = _durable_fact_keys_for("other")
    assert FACT_BHK in other_keys
    assert FACT_DOCTOR_PREFERENCE in other_keys


def test_flow_slot_aliases_cover_all_domains() -> None:
    from app.services.conversational_memory import fact_for_flow_slot

    assert fact_for_flow_slot("appointment_date") == FACT_VISIT_DATE
    assert fact_for_flow_slot("order_number") == FACT_ORDER_ID
    assert fact_for_flow_slot("check_in_date") == FACT_CHECK_IN
    assert fact_for_flow_slot("party_size") == FACT_PARTY_SIZE


def test_extractor_captures_project_from_interest_cue() -> None:
    """The caller naming a project early must be remembered so the booking
    flow doesn't re-ask 'which project?'."""
    m = ConversationalMemory()
    m.merge_text("I'm interested in Raghava Skyline Residences", turn_index=1)
    assert m.snapshot()[FACT_PROPERTY] == "Raghava Skyline Residences"


def test_extractor_captures_project_from_project_keyword() -> None:
    m = ConversationalMemory()
    m.merge_text("tell me about the green meadows project", turn_index=1)
    assert m.snapshot()[FACT_PROPERTY] == "Green Meadows"


def test_extractor_property_ignores_generic_phrases() -> None:
    m = ConversationalMemory()
    m.merge_text("interested in the details and pricing", turn_index=1)
    assert FACT_PROPERTY not in m.snapshot()


def test_project_slot_hydrates_from_property_fact() -> None:
    """project_name/project must map to FACT_PROPERTY so the booking flow's
    project slot pre-fills from memory."""
    assert FLOW_SLOT_TO_FACT["project_name"] == FACT_PROPERTY
    assert FLOW_SLOT_TO_FACT["project"] == FACT_PROPERTY

    m = ConversationalMemory()
    m.merge_text("interested in Raghava Urban Nest", turn_index=1)
    merged = hydrate_flow_collected({}, m)
    assert merged["project_name"] == "Raghava Urban Nest"
