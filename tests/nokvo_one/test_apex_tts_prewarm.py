"""TTS pre-warm at APEX campaign creation.

Every scripted line (intro chunks / questions / outro) is synthesized once per
pre-translated language with cache=True and EXACTLY the params its call-time
site uses (cache-key parity), so even the campaign's first call in each
language plays cached audio. Best-effort: synthesis failures never propagate.
"""
from __future__ import annotations

import asyncio

import app.services.apex_tts_prewarm as pw
from app.services.prosody import prosody_for


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _questionnaire():
    return {
        "questions": [
            {"id": "q1", "type": "intent", "text": "Are you interested?",
             "text_i18n": {"en": "Q1 EN", "hi": "Q1 HI", "te": "Q1 TE"}},
        ],
        "threshold": 1,
        "intro": "Hello there. Got a minute?",
        # The en intro is multi-tone: _play_opener splits it into two chunks
        # (warm + question), each synthesized separately with its own prosody.
        "intro_i18n": {
            "en": "[warm]Hello there.[/warm] [question]Got a minute?[/question]",
            "hi": "HI intro",
            "te": "TE intro",
        },
        "outro": "Thanks, bye.",
        "outro_i18n": {"en": "OUT EN", "hi": "OUT HI", "te": "OUT TE"},
    }


def _capture(monkeypatch):
    calls: list[dict] = []

    async def fake_synth(tenant_res, text, *, language=None, cache=False, **kw):
        calls.append({"text": text, "language": language, "cache": cache, **kw})
        return {"audios": ["x"]}

    monkeypatch.setattr(pw.SarvamVoiceService, "synthesize", staticmethod(fake_synth))
    return calls


def test_prewarms_every_line_per_language_with_call_time_params(monkeypatch):
    calls = _capture(monkeypatch)
    _run(pw.prewarm_campaign_tts(object(), _questionnaire()))

    assert calls and all(c["cache"] for c in calls)
    assert {c["language"] for c in calls} == {"en", "hi", "te"}

    # Questions mirror _deliver_verbatim_question: prosody_for("question").
    qp = prosody_for("question")
    q_te = next(c for c in calls if c["text"] == "Q1 TE")
    assert q_te["language"] == "te"
    assert (q_te["pace"], q_te["pitch"], q_te["loudness"]) == (qp.pace, qp.pitch, qp.loudness)

    # The outro close passes NO prosody args — the pre-warm must not either.
    out_hi = next(c for c in calls if c["text"] == "OUT HI")
    assert "pace" not in out_hi and "pitch" not in out_hi and "loudness" not in out_hi

    # Raw text goes straight through (synthesize normalizes internally; a second
    # normalization here would change the cache key).
    assert any(c["text"] == "Q1 EN" for c in calls)


def test_multi_tone_intro_prewarms_one_call_per_chunk(monkeypatch):
    calls = _capture(monkeypatch)
    _run(pw.prewarm_campaign_tts(object(), _questionnaire()))

    warm, question = prosody_for("warm"), prosody_for("question")
    chunk1 = next(c for c in calls if c["text"] == "Hello there." and c["language"] == "en")
    chunk2 = next(c for c in calls if c["text"] == "Got a minute?" and c["language"] == "en")
    assert (chunk1["pace"], chunk1["pitch"], chunk1["loudness"]) == (warm.pace, warm.pitch, warm.loudness)
    assert (chunk2["pace"], chunk2["pitch"], chunk2["loudness"]) == (question.pace, question.pitch, question.loudness)
    # The whole tagged string must never be synthesized as one call.
    assert not any("[warm]" in c["text"] for c in calls)

    # An untagged intro (hi/te here) is voiced as ONE warm chunk, like _play_opener.
    intro_te = next(c for c in calls if c["text"] == "TE intro")
    assert (intro_te["pace"], intro_te["pitch"], intro_te["loudness"]) == (warm.pace, warm.pitch, warm.loudness)


def test_missing_languages_are_skipped(monkeypatch):
    calls = _capture(monkeypatch)
    q = {
        "questions": [{"id": "q1", "text": "Hi?", "text_i18n": {"en": "Only EN"}}],
        "threshold": 1,
    }
    _run(pw.prewarm_campaign_tts(object(), q))
    assert [c["text"] for c in calls] == ["Only EN"]


def test_synthesis_failure_does_not_raise(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("tts down")

    monkeypatch.setattr(pw.SarvamVoiceService, "synthesize", staticmethod(boom))
    _run(pw.prewarm_campaign_tts(object(), _questionnaire()))  # must not raise


def test_no_questionnaire_is_a_noop(monkeypatch):
    calls = _capture(monkeypatch)
    _run(pw.prewarm_campaign_tts(object(), None))
    assert calls == []
