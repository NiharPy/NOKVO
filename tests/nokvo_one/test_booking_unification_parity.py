"""P1 booking-unification parity gate.

When ``unified_booking_engine`` is flipped ON for a clinic, the bespoke
``evaluate_voice_turn_policy`` FSM is bypassed and the turn falls through to the
generic engine + the answer-flow triage layer (``clinic_agent_fsm.current_mode`` /
``detect_urgent_symptoms``), which runs regardless of the booking engine.

The SAFETY invariant that makes the flip non-regressive: the engine-independent
``detect_urgent_symptoms`` must catch EVERYTHING the bespoke deterministic urgent
path (``voice_turn_policy._URGENT_SYMPTOM_RE``) caught — i.e. it must be a superset.
Historically it was NOT (bespoke = eye emergencies, engine-independent = general
emergencies), so flipping the flag at an eye clinic would drop eye-emergency triage.
This test locks the superset invariant so the flip stays safe.
"""
from __future__ import annotations

from app.services.clinic_agent_fsm import detect_urgent_symptoms
from app.services.voice_turn_policy import _URGENT_SYMPTOM_RE


# Eye/ophthalmology emergencies the bespoke deterministic path caught.
EYE_EMERGENCIES = [
    "I suddenly have vision loss",
    "sudden blindness in my right eye",
    "I have severe eye pain",
    "there was an eye injury at work",
    "a chemical splash went into my eye",
    "there's an object stuck in my eye",
    "I see blood in my eye",
    "I'm seeing flashes of light",
    "sudden increase in floaters",
    "a curtain-like shadow over my vision",
    "swelling around my eye with fever",
    "my contact lenses are causing pain and redness",
]

# General medical emergencies the engine-independent detector already caught.
GENERAL_EMERGENCIES = [
    "I have chest pain",
    "I can't breathe",
    "he is unconscious",
    "she had a seizure",
    "this looks like a stroke",
    "I think it's a heart attack",
    "there is severe bleeding",
]

# Routine eye-clinic phrasing that must NOT be flagged urgent (false-positive guard).
NON_URGENT = [
    "I'd like to book an eye checkup",
    "my eyes are a bit red",
    "I need new spectacles",
    "my vision is a little blurred when reading",
    "what is the consultation fee",
]


def test_engine_independent_detector_covers_general_emergencies():
    for phrase in GENERAL_EMERGENCIES:
        assert detect_urgent_symptoms(phrase) is True, phrase


def test_engine_independent_detector_covers_eye_emergencies():
    # The flip-safety invariant: eye emergencies (bespoke-only, historically) must
    # also be caught by the engine-independent path.
    for phrase in EYE_EMERGENCIES:
        assert detect_urgent_symptoms(phrase) is True, phrase


def test_detector_is_superset_of_bespoke_urgent_path():
    # Formal parity invariant: anything the retiring bespoke regex matched, the
    # engine-independent detector must match too — else the flag flip regresses safety.
    for phrase in EYE_EMERGENCIES + GENERAL_EMERGENCIES:
        if _URGENT_SYMPTOM_RE.search(phrase):
            assert detect_urgent_symptoms(phrase) is True, phrase


def test_no_false_positives_on_routine_phrases():
    for phrase in NON_URGENT:
        assert detect_urgent_symptoms(phrase) is False, phrase
