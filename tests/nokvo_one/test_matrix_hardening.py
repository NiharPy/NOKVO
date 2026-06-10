"""Regression tests for the test-matrix findings (#18 opt-out, #11 objection, #19 names)."""
from __future__ import annotations

from app.services.conversational_memory import _OPT_OUT_RE, ConversationalMemory as CM
from app.services.tool_flow_policy import _extract_value


def _objections(text: str) -> list[str]:
    cm = CM()
    cm.merge_text(text, turn_index=0, language="en", role="user", business_type="real_estate")
    return [o.get("code") for o in (cm.objections or []) if isinstance(o, dict)]


# ── #18: multilingual opt-out (compliance) ──────────────────────────────────


def test_optout_multilingual():
    for t in ("don't call me again", "stop calling me", "remove me from your list",
              "mujhe call mat karo", "phone mat karo", "dobara call mat karna",
              "naaku call cheyakandi", "call cheyyaku", "call मत करो", "కాల్ చేయకండి"):
        assert _OPT_OUT_RE.search(t), f"opt-out not detected: {t!r}"


def test_optout_no_false_positive():
    for t in ("hello I want a site visit", "can you call me tomorrow at 4pm", "I'm interested"):
        assert not _OPT_OUT_RE.search(t), f"false opt-out: {t!r}"


# ── #11: "can't afford" is a price objection ────────────────────────────────


def test_price_objection_phrasings():
    for t in ("that's too expensive", "I can't afford 80 lakhs", "too much",
              "out of my budget", "beyond my budget", "bahut mehenga"):
        assert "price_concern" in _objections(t), f"missed price objection: {t!r}"
    assert "price_concern" not in _objections("sounds great, let's do it")


# ── #19: transliterated Hindi/Telugu name lead-ins + long names ─────────────


def test_translit_name_leadins_and_long_names():
    assert _extract_value("Mera naam Lakshmi Narasimha Rao", "name", "name") == "Lakshmi Narasimha Rao"
    assert _extract_value("naa peru Venkata Subramanyam", "name", "name") == "Venkata Subramanyam"
    assert _extract_value("my name is Lakshmi Narasimha Rao", "name", "name") == "Lakshmi Narasimha Rao"
    assert _extract_value("Lakshmi Narasimha Rao", "name", "name") == "Lakshmi Narasimha Rao"
