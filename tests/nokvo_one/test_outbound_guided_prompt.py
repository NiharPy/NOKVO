"""Regression tests for the guided outbound setup + filler reduction + te/hi parity.

Covers the changes from the "guided campaign setup" work:
  * the engineered system prompt interpolates the operator-supplied details
    (company / caller) instead of rendering generic "the company";
  * the outbound prompt forbids vocalized fillers (um/uh/mm/hmm/…);
  * the te/hi outbound few-shots reached scenario parity with English
    (what-is-this / name capture / callback) and carry no Malayalam script.
"""
from app.services.agent_outbound_context import (
    OutboundCampaignContext,
    build_agent_config,
    compose_outbound_system_section,
    generate_outbound_opener_text,
    _call_purpose_line,
    _spoken_pitch,
    _OUTBOUND_BASE_TEMPLATE,
)
from app.services.language_style import outbound_fewshot
from app.services.pipeline.message_composer import compose_rag_messages


def _ctx(language: str) -> OutboundCampaignContext:
    cfg = build_agent_config(
        agent_prompt="New 3BHK in Kollur, 85L onward, free registration this month.",
        pitch_summary="New 3BHK in Kollur, 85L onward, free registration this month.",
        company_name="Raghava Estates",
        caller_name="Riya",
        language=language,
        objectives=["site_visit", "lead"],
        tone="warm, direct",
    )
    return OutboundCampaignContext(
        campaign_id="c1", name="Dec outreach", goal="", agent_prompt=cfg["agent_prompt"],
        objectives=cfg["objectives"], exit_conditions=cfg["exit_conditions"], tone=cfg["tone"],
        doc_text=None, caller_name=cfg["caller_name"], company_name=cfg["company_name"],
        pitch_summary=cfg["pitch_summary"], objective=cfg["objective"],
    )


def _has_malayalam(text: str) -> bool:
    return any("ഀ" <= ch <= "ൿ" for ch in text)


def test_guided_details_interpolated_into_system_prompt():
    section = compose_outbound_system_section(_ctx("en"), language="en", business_type="real_estate")
    assert "Raghava Estates" in section  # company fed, not "the company"
    assert "Riya" in section
    assert "Kollur" in section  # pitch reaches the "what is this?" purpose line


def test_outbound_prompt_forbids_vocalized_fillers():
    section = compose_outbound_system_section(_ctx("en"), language="en", business_type="real_estate")
    assert "NO VOCALIZED FILLERS" in section
    # the old filler-y acknowledgment example is gone from the scaffold
    assert "Mm, lovely" not in _OUTBOUND_BASE_TEMPLATE


def test_outbound_message_has_crisp_delivery_and_language_note():
    te = compose_rag_messages("hi", [], language="te", history=[], outbound_context=_ctx("te"),
                              business_type="real_estate")[0]["content"]
    en = compose_rag_messages("hi", [], language="en", history=[], outbound_context=_ctx("en"),
                              business_type="real_estate")[0]["content"]
    assert "CRISP DELIVERY" in te and "CRISP DELIVERY" in en
    # the English few-shots are flagged as format-only for te, not for en
    assert "FORMAT, NOT LANGUAGE" in te
    assert "FORMAT, NOT LANGUAGE" not in en


def test_te_hi_fewshots_reach_scenario_parity_in_native_script():
    te = outbound_fewshot("te")
    hi = outbound_fewshot("hi")
    # new scenarios present (what-is-this, name capture, callback)
    assert "ఇది ఏంటి" in te and "Nihar" in te and "2 hours" in te
    assert "किस बारे में" in hi and "Nihar" in hi and "2 hours" in hi
    # native script, no Malayalam leakage anywhere in the outbound few-shots
    assert not _has_malayalam(te) and not _has_malayalam(hi)
    assert not _has_malayalam(_OUTBOUND_BASE_TEMPLATE)


# ── Opener must NOT read the campaign content/prompt aloud ───────────────────

_BLOB = (
    "New 3BHK gated community in Kollur, 85L onward, ready by December. "
    "Offer: free registration this month. Mention the clubhouse and metro connectivity."
)


def _blob_ctx(language: str) -> OutboundCampaignContext:
    # Mimics a campaign whose long content blob leaked into pitch_summary
    # (as pre-fix campaigns have stored on disk).
    return OutboundCampaignContext(
        campaign_id="c1", name="Dec", goal="", agent_prompt=_BLOB, objectives=["site_visit"],
        exit_conditions=[], tone="warm", doc_text=None,
        caller_name="Riya", company_name="Raghava Estates", pitch_summary=_BLOB,
        objective="lead_qualification",
    )


def test_long_pitch_is_never_spoken():
    assert _spoken_pitch(_blob_ctx("en")) == ""
    # a genuinely short pitch is still spoken (backwards compat for old campaigns)
    short = OutboundCampaignContext(
        campaign_id="c2", name="n", goal="", agent_prompt="x", objectives=[], exit_conditions=[],
        tone=None, doc_text=None, company_name="Acme", pitch_summary="quick home options in Kollur",
    )
    assert _spoken_pitch(short) == "quick home options in Kollur"


def test_opener_does_not_read_the_content_blob():
    for lang in ("en", "te", "hi"):
        opener = generate_outbound_opener_text(_blob_ctx(lang), language=lang)
        assert "clubhouse" not in opener and "registration" not in opener, lang
        assert "Raghava Estates" in opener  # still campaign-aware via company


def test_telugu_opener_is_clean_native_script():
    te = generate_outbound_opener_text(_blob_ctx("te"), language="te")
    assert "మాట్లాడగలరా" in te  # native Telugu close
    assert not _has_malayalam(te)


def test_purpose_line_does_not_dump_the_blob():
    line = _call_purpose_line(_blob_ctx("en"))
    assert "clubhouse" not in line and "Raghava Estates" in line
