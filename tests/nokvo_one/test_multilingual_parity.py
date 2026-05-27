"""Telugu / Hindi parity tests.

These guard the things that broke the older multilingual experience:

  1. System prompts must mandate natural English code-switching (the old
     "Do not mix languages" wording forced Sanskritised / news-anchor
     register and is now banned).
  2. The te / hi style block must be injected into both the inbound RAG
     prompt and the smalltalk prompt.
  3. The outbound opener must be localised to the campaign's language so
     calls don't start every conversation in English.
  4. ``build_agent_config`` must round-trip the campaign language so the
     Exotel outbound WebSocket can pick it up.
  5. Soniox language hints must include English as a secondary hint for
     Indian-language calls (real callers mid-utterance code-switch).
"""
from __future__ import annotations

from app.services.agent_outbound_context import (
    OutboundCampaignContext,
    build_agent_config,
    generate_outbound_opener_text,
)
from app.services.language_style import (
    has_detailed_guidance,
    outbound_fewshot,
    style_guidance,
)
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.soniox_stt_service import SonioxSTTService


def _system_prompt(language: str, *, outbound: OutboundCampaignContext | None = None) -> str:
    messages = NokvoOneVoicePipeline._messages(
        "Where is the clinic?",
        [{"text": "Clinic is at KPHB, Hyderabad. Hours 9am-6pm."}],
        language=language,
        history=[],
        company_name="ZapEats",
        outbound_context=outbound,
    )
    assert messages and messages[0]["role"] == "system"
    return messages[0]["content"]


def _smalltalk_prompt(language: str) -> str:
    messages = NokvoOneVoicePipeline._messages_smalltalk(
        "namaste",
        language=language,
        history=[],
        company_name="ZapEats",
    )
    assert messages and messages[0]["role"] == "system"
    return messages[0]["content"]


def test_style_guidance_emits_per_language_block():
    assert "TELUGU STYLE" in style_guidance("te")
    assert "HINDI STYLE" in style_guidance("hi")
    assert style_guidance("en") == ""  # English uses the prompt's English-native register
    assert "LANGUAGE STYLE" in style_guidance("ta")  # generic fallback for other Indian langs


def test_has_detailed_guidance_only_for_te_hi():
    assert has_detailed_guidance("te")
    assert has_detailed_guidance("hi")
    assert not has_detailed_guidance("ta")
    assert not has_detailed_guidance("en")


def test_telugu_system_prompt_includes_native_register_and_code_switching():
    prompt = _system_prompt("te")
    # Telugu-specific register guidance is present.
    assert "TELUGU STYLE" in prompt
    # Code-switching is mandated, not banned. The old "Do not mix languages"
    # phrase must not survive — it produced Sanskritised replies.
    assert "Do not mix languages" not in prompt
    assert "code-switch" in prompt.lower() or "code-switching" in prompt.lower()
    # English loanwords are explicitly listed so the model keeps them in English.
    assert "order" in prompt.lower()
    assert "refund" in prompt.lower()


def test_hindi_system_prompt_includes_native_register_and_code_switching():
    prompt = _system_prompt("hi")
    assert "HINDI STYLE" in prompt
    assert "Do not mix languages" not in prompt
    assert "code-switching" in prompt.lower()


def test_english_system_prompt_skips_indic_style_block():
    prompt = _system_prompt("en")
    # English path should not carry the per-language style block — it's
    # English by default. But the prompt must still mention code-switching
    # as a general principle (so mixed callers' replies still work).
    assert "TELUGU STYLE" not in prompt
    assert "HINDI STYLE" not in prompt


def test_smalltalk_prompt_carries_style_block_for_te_hi():
    te_prompt = _smalltalk_prompt("te")
    hi_prompt = _smalltalk_prompt("hi")
    assert "TELUGU STYLE" in te_prompt
    assert "HINDI STYLE" in hi_prompt
    # Smalltalk path must not retain the old "Do not switch languages" line
    # that prevented mid-call code-switching acknowledgments.
    assert "Do not switch languages" not in te_prompt
    assert "Do not switch languages" not in hi_prompt


def test_outbound_fewshot_present_for_te_hi():
    assert "TELUGU FEW-SHOT" in outbound_fewshot("te")
    assert "HINDI FEW-SHOT" in outbound_fewshot("hi")
    assert outbound_fewshot("en") == ""
    assert outbound_fewshot("ta") == ""


def test_outbound_system_prompt_includes_language_fewshot():
    outbound = OutboundCampaignContext(
        campaign_id="c1",
        name="Telugu campaign",
        goal="Book a site visit",
        agent_prompt="",
        objectives=["confirm interest", "schedule a visit"],
        exit_conditions=[],
        tone=None,
        doc_text=None,
        caller_name="Riya",
        company_name="Raghava Skyline",
        pitch_summary="3BHK apartments in Gachibowli",
    )
    te_prompt = _system_prompt("te", outbound=outbound)
    hi_prompt = _system_prompt("hi", outbound=outbound)
    assert "TELUGU FEW-SHOT" in te_prompt
    assert "TELUGU STYLE" in te_prompt
    assert "HINDI FEW-SHOT" in hi_prompt
    assert "HINDI STYLE" in hi_prompt


def test_generate_outbound_opener_text_localises_te_hi():
    ctx = OutboundCampaignContext(
        campaign_id="c1",
        name="Demo",
        goal="",
        agent_prompt="",
        objectives=[],
        exit_conditions=[],
        tone=None,
        doc_text=None,
        caller_name="Riya",
        company_name="Raghava Skyline",
        pitch_summary="3BHK apartments in Gachibowli",
    )
    en = generate_outbound_opener_text(ctx, language="en")
    te = generate_outbound_opener_text(ctx, language="te")
    hi = generate_outbound_opener_text(ctx, language="hi")
    # English opener is the canonical "Hi, this is …" line.
    assert "Hi, this is Riya" in en
    # Telugu opener uses transliterated Telugu — natural for code-switched
    # phone openers — and keeps the company name in English mid-line.
    assert "nenu Riya" in te.lower() or "nenu riya" in te.lower()
    assert "Raghava Skyline" in te
    # Hindi opener starts with "Namaste" + transliterated Hindi cadence.
    assert "Namaste" in hi
    assert "Raghava Skyline" in hi
    # All three carry the prosody tags so stream_prosody_chunks can render.
    for opener in (en, te, hi):
        assert "[warm]" in opener
        assert "[question]" in opener


def test_build_agent_config_round_trips_language():
    cfg = build_agent_config(language="te")
    assert cfg["language"] == "te"
    cfg2 = build_agent_config(language="te-IN")
    assert cfg2["language"] == "te"
    cfg3 = build_agent_config()  # legacy callers — no language hint
    assert cfg3["language"] == ""
    cfg4 = build_agent_config(language="unknown")
    assert cfg4["language"] == ""


def test_soniox_language_hints_add_english_for_indian_languages():
    # Single-language English calls stay as ["en"] — no double-hint penalty.
    assert SonioxSTTService._language_hints("en") == ["en"]
    # Telugu / Hindi must add English as a secondary hint so Soniox doesn't
    # garble mid-utterance code-switched English tokens (order, refund, ₹500).
    assert SonioxSTTService._language_hints("te") == ["te", "en"]
    assert SonioxSTTService._language_hints("hi") == ["hi", "en"]
    assert SonioxSTTService._language_hints("ta") == ["ta", "en"]
    # BCP-47 normalises to short.
    assert SonioxSTTService._language_hints("hi-IN") == ["hi", "en"]
    # None / empty stays empty.
    assert SonioxSTTService._language_hints(None) == []
    assert SonioxSTTService._language_hints("") == []
