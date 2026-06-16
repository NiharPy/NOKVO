"""TTS number normalization + transliteration repair.

`normalize_text_for_tts` rewrites EVERY number to English words for the audio
only (the on-screen transcript keeps "₹2.45Cr"/digits), because bulbul:v3
otherwise reads digits in the voice's own language (Telugu "మూడు" for 3) and
reads "₹" split across the decimal. It also repairs recurring transliterated
loanwords ("వాట్సాప్" → WhatsApp). Language-agnostic.
"""
from __future__ import annotations

from app.services.sarvam_voice_service import normalize_text_for_tts as n


# ── currency ─────────────────────────────────────────────────────────────────


def test_rupee_crore_decimal():
    assert n("₹2.45Cr") == "two point four five crore rupees"
    assert n("starting ₹2.45Cr today") == "starting two point four five crore rupees today"


def test_decimal_point_spaced_by_model():
    # The LLM sometimes emits "₹2. 45Cr" (space after the dot) — collapse it.
    assert n("price roughly ₹2. 45Cr only") == "price roughly two point four five crore rupees only"
    assert n("₹2 . 45 crore") == "two point four five crore rupees"


def test_rupee_plain_and_lakh():
    assert n("₹500 consultation") == "five hundred rupees consultation"
    assert n("₹50L for the 2 BHK") == "fifty lakh rupees for the two BHK"
    assert n("₹50k token") == "fifty thousand rupees token"


def test_rs_and_inr_prefixes():
    assert n("Rs. 1.5 crore") == "one point five crore rupees"
    assert n("INR 2.45 cr") == "two point four five crore rupees"


def test_number_unit_without_currency_mark():
    assert n("budget around 2.45 crore range") == "budget around two point four five crore range"


# ── times ────────────────────────────────────────────────────────────────────


def test_times_spoken_in_english():
    assert n("9 AM") == "nine AM"
    assert n("11:00 AM") == "eleven AM"
    assert n("12 PM") == "twelve PM"
    assert n("11:30 PM") == "eleven thirty PM"
    assert n("meet at 10:00 AM") == "meet at ten AM"


# ── phone / long digit runs ────────────────────────────────────────────────


def test_phone_spelled_digit_by_digit():
    assert n("call 9876543210") == "call nine eight seven six five four three two one zero"
    assert n("7569672503") == "seven five six nine six seven two five zero three"


# ── plain integers + decimals + years ──────────────────────────────────────


def test_integers_and_bhk_to_words():
    assert n("3 BHK and 4 BHK") == "three BHK and four BHK"
    assert n("500 units") == "five hundred units"


def test_bare_decimal_to_words():
    assert n("around 2.45 only") == "around two point four five only"


def test_year_spoken_naturally():
    assert n("possession Dec 2026") == "possession Dec twenty twenty six"


def test_embedded_digits_left_alone():
    # boundary-guarded: a digit inside an alphanumeric token must not convert.
    assert n("B2B and Web3 are fine") == "B2B and Web3 are fine"


# ── native-script sentence + preservation ──────────────────────────────────


def test_inside_native_script_sentence():
    out = n("Price roughly ₹2.45Cr నుంచి start అవుతుంది.")
    assert "two point four five crore rupees" in out
    assert "నుంచి start అవుతుంది" in out  # Telugu words preserved


def test_no_op_when_no_numbers_or_translit():
    s = "మీకు ఏం details కావాలి?"
    assert n(s) == s


# ── transliteration backstop ────────────────────────────────────────────────


def test_transliterated_loanwords_repaired():
    assert "WhatsApp" in n("వాట్సాప్ లో brochure పంపిస్తా")
    assert "team confirm" in n("टीम confirm చేస్తుంది")  # Devanagari "team" in te reply
    assert "hang up" in n("నేను హ్యాంగ్ అప్ అయ్యాక")
    assert "brochure" in n("బ్రోచర్ పంపిస్తా")
