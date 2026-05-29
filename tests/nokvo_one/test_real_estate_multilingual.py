"""Telugu / Hindi parity for the real-estate extraction + intent layer.

English got richer (paraphrased intents, project capture, fuzzy areas); these
tests lock Telugu and Hindi to the same behaviour — relative date/time parsing,
visit/enquiry intent, location capture (postposition order), memory extraction,
and the localized project-enumeration question.
"""
from __future__ import annotations

import pytest

from app.services.voice_turn_policy import (
    extract_turn_entities,
    normalize_relative_datetime_text,
)
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.tool_flow_policy import _infer_domain_slots, _start_flow_key
from app.services.tool_flow_questions import (
    build_tool_flow_questions,
    format_field_questions_prompt,
)
from app.services.conversational_memory import (
    ConversationalMemory,
    FACT_LOCATION,
    FACT_PROPERTY,
    FACT_PURPOSE,
    FACT_TIMELINE,
    FACT_VISIT_DATE,
)


# ─────────── Relative date / time normalization + parsing ───────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("kal", "tomorrow"),
        ("repu", "tomorrow"),
        ("aaj", "today"),
        ("parso", "day after tomorrow"),
        ("ellundi", "day after tomorrow"),
        ("రేపు", "tomorrow"),
        ("आज", "today"),
        ("kal subah", "tomorrow morning"),
        ("repu sayantram", "tomorrow evening"),
        ("శుక్రవారం shaam", "శుక్రవారం evening"),
    ],
)
def test_normalize_relative_datetime(text, expected):
    assert normalize_relative_datetime_text(text) == expected


def test_appointment_date_parses_hi_te_relative():
    assert NokvoOneVoicePipeline._parse_appointment_date("repu") == \
        NokvoOneVoicePipeline._parse_appointment_date("tomorrow")
    assert NokvoOneVoicePipeline._parse_appointment_date("aaj") == \
        NokvoOneVoicePipeline._parse_appointment_date("today")
    assert NokvoOneVoicePipeline._parse_appointment_date("parso") == \
        NokvoOneVoicePipeline._parse_appointment_date("day after tomorrow")


def test_appointment_time_parses_hi_te_part_of_day():
    assert NokvoOneVoicePipeline._parse_appointment_time("shaam") == \
        NokvoOneVoicePipeline._parse_appointment_time("evening")
    assert NokvoOneVoicePipeline._parse_appointment_time("subah") == \
        NokvoOneVoicePipeline._parse_appointment_time("morning")
    assert NokvoOneVoicePipeline._parse_appointment_time("సాయంత్రం") == \
        NokvoOneVoicePipeline._parse_appointment_time("evening")


def test_extract_turn_entities_hi_te_date_time():
    ent = extract_turn_entities("repu shaam")
    assert ent.get("date_text") == "tomorrow"
    assert ent.get("time_text") == "evening"
    ent2 = extract_turn_entities("రేపు ఉదయం")
    assert ent2.get("date_text") == "tomorrow"
    assert ent2.get("time_text") == "morning"


# ─────────── Intent parity ───────────


@pytest.mark.parametrize(
    "text",
    [
        "Skyline chudali",
        "main flat dekhne aana chahta hoon",
        "property చూడాలని ఉంది",
        "ghar dikhao",
        "साइट विजिट करना है",
    ],
)
def test_visit_intent_hi_te(text):
    assert _start_flow_key(text, "real_estate", []) == "real_estate_site_visit"


@pytest.mark.parametrize(
    "text",
    [
        "mujhe jankari chahiye",
        "vivaralu kavali",
        "price enta",  # script path: ధర/రేటు also covered below
        "ధర చెప్పండి",
        "मुझे डिटेल्स भेजिए",
    ],
)
def test_enquiry_intent_hi_te(text):
    assert _start_flow_key(text, "real_estate", []) == "leads_create"


def test_visit_beats_lead_in_hi_te():
    # "interested" + "chudali" → visit (more specific) wins.
    assert _start_flow_key("interest undi, Skyline chudali", "real_estate", []) \
        == "real_estate_site_visit"


# ─────────── Location inference (postposition order) ───────────


def _loc_bundle():
    return {"flows": {"leads_create": {"slots": [{"key": "location", "kind": "location"}]}}}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Kondapur mein", "Kondapur"),
        ("Gachibowli ke paas", "Gachibowli"),
        ("Kokapet లో", "Kokapet"),
        ("Kondapur में", "Kondapur"),
    ],
)
def test_location_inference_hi_te_postposition(text, expected):
    collected: dict = {}
    _infer_domain_slots(text, "real_estate", _loc_bundle(), "leads_create", collected)
    assert collected.get("location") == expected


def test_memory_location_hi_te_postposition():
    m = ConversationalMemory()
    m.merge_text("Gachibowli mein 3 BHK", turn_index=1)
    assert m.snapshot().get(FACT_LOCATION) == "Gachibowli"


# ─────────── Memory extractors ───────────


def test_memory_property_hi_te():
    m = ConversationalMemory()
    m.merge_text("Skyline gurinchi cheppandi", turn_index=1)
    assert m.snapshot().get(FACT_PROPERTY) == "Skyline"
    m2 = ConversationalMemory()
    m2.merge_text("Green Meadows ke baare mein batao", turn_index=1)
    assert m2.snapshot().get(FACT_PROPERTY) == "Green Meadows"


def test_memory_purpose_hi_te():
    m = ConversationalMemory()
    m.merge_text("rehne ke liye chahiye", turn_index=1)
    assert m.snapshot().get(FACT_PURPOSE) == "self-use"
    m2 = ConversationalMemory()
    m2.merge_text("pettubadi kosam", turn_index=1)
    assert m2.snapshot().get(FACT_PURPOSE) == "investment"


def test_memory_timeline_hi_te():
    m = ConversationalMemory()
    m.merge_text("turant chahiye", turn_index=1)
    assert m.snapshot().get(FACT_TIMELINE)
    m2 = ConversationalMemory()
    m2.merge_text("ventane kavali", turn_index=1)
    assert m2.snapshot().get(FACT_TIMELINE)


def test_memory_visit_date_hi_te():
    m = ConversationalMemory()
    m.merge_text("repu vasta", turn_index=1)
    assert m.snapshot().get(FACT_VISIT_DATE) == "repu"
    m2 = ConversationalMemory()
    m2.merge_text("कल आऊंगा", turn_index=1)
    assert m2.snapshot().get(FACT_VISIT_DATE) == "कल"


# ─────────── Localized project-enumeration question ───────────


def _project_question_for(language: str) -> str:
    catalog = build_tool_flow_questions("real_estate")
    prompt = format_field_questions_prompt(
        catalog, language=language, project_names=["Raghava Skyline", "Raghava Green Meadows"]
    )
    return prompt


def test_project_question_localized():
    assert "Which project would you like to visit" in _project_question_for("en")
    assert "आप कौन सा project visit करना चाहेंगे" in _project_question_for("hi")
    assert "మీరు ఏ project visit చేయాలనుకుంటున్నారు" in _project_question_for("te")
