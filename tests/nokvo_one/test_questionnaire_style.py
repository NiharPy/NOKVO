"""Conversation-style rewrite of an APEX questionnaire (style-rewrite preview).

rewrite_questionnaire restyles question/intro/outro WORDING via one LLM call
(mocked), keeps originals in *_source, drops stale i18n for changed lines, and
returns per-line fallback warnings so a partially styled script is never
silent. Scripted restores the originals without an LLM call. Style + sources
round-trip _coerce_questionnaire, and micro-ack pools follow the style.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import app.services.questionnaire_style as qs


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _questionnaire():
    return {
        "questions": [
            {"id": "q1", "type": "intent", "text": "Are you interested?"},
            {"id": "q2", "type": "answer", "text": "What is your budget?", "desired_answer": "50L"},
        ],
        "threshold": 1,
        "intro": "Hi, this is Riya from Raghava.",
        "outro": "Thank you for your time.",
    }


def _mock_llm(monkeypatch, reply, calls=None):
    async def fake_chat(messages, **kwargs):
        if calls is not None:
            calls.append(messages)
        return reply

    from app.services import llm_pool

    monkeypatch.setattr(llm_pool.LLMPoolClient, "chat", classmethod(lambda cls, m, **k: fake_chat(m, **k)))


def _styled_reply(overrides=None):
    base = {
        "q1": "Just checking — would this be something you're interested in?",
        "q2": "Could I ask roughly what budget you're working with?",
        "__intro__": "Hi! Riya here — hope I'm not catching you at a bad time.",
        "__outro__": "Thanks a ton for your time — take care!",
    }
    base.update(overrides or {})
    return json.dumps({"items": [{"id": k, "text": v} for k, v in base.items()]})


def test_rewrites_text_and_keeps_source(monkeypatch):
    _mock_llm(monkeypatch, _styled_reply())
    q, warnings = _run(qs.rewrite_questionnaire(_questionnaire(), "human"))
    assert warnings == []
    assert q["style"] == "human"
    assert q["questions"][0]["text"].startswith("Just checking")
    assert q["questions"][0]["text_source"] == "Are you interested?"
    assert q["questions"][1]["text_source"] == "What is your budget?"
    assert q["intro"].startswith("Hi! Riya here")
    assert q["intro_source"] == "Hi, this is Riya from Raghava."
    assert q["outro_source"] == "Thank you for your time."
    # Scoring fields are never touched by the rewrite.
    assert q["questions"][1]["desired_answer"] == "50L"


def test_drops_stale_i18n_for_changed_lines(monkeypatch):
    _mock_llm(monkeypatch, _styled_reply())
    q = _questionnaire()
    q["questions"][0]["text_i18n"] = {"en": "old", "hi": "x", "te": "y"}
    q["intro_i18n"] = {"en": "old intro"}
    out, _ = _run(qs.rewrite_questionnaire(q, "human"))
    # Translations described the OLD wording — they must not survive a restyle.
    assert "text_i18n" not in out["questions"][0]
    assert "intro_i18n" not in out


def test_scripted_restores_and_strips_without_llm(monkeypatch):
    called = {"n": 0}

    async def counting(messages, **kwargs):
        called["n"] += 1
        return "{}"

    from app.services import llm_pool

    monkeypatch.setattr(llm_pool.LLMPoolClient, "chat", classmethod(lambda cls, m, **k: counting(m, **k)))
    q = {
        "style": "human",
        "questions": [
            {"id": "q1", "type": "intent", "text": "Styled?", "text_source": "Original?", "text_i18n": {"en": "styled"}}
        ],
        "threshold": 1,
        "intro": "Styled intro.",
        "intro_source": "Original intro.",
    }
    out, warnings = _run(qs.rewrite_questionnaire(q, "scripted"))
    assert called["n"] == 0
    assert warnings == []
    assert out["questions"][0]["text"] == "Original?"
    assert "text_source" not in out["questions"][0]
    assert "text_i18n" not in out["questions"][0]  # described the styled text
    assert out["intro"] == "Original intro."
    assert "intro_source" not in out
    assert "style" not in out


def test_restyle_regenerates_from_source(monkeypatch):
    calls = []
    _mock_llm(monkeypatch, _styled_reply(), calls=calls)
    q, _ = _run(qs.rewrite_questionnaire(_questionnaire(), "human"))
    # Switching Human → Luxury must rewrite the ADMIN's wording, not Human's.
    _mock_llm(
        monkeypatch,
        _styled_reply({"q2": "May I ask what investment range you're considering?"}),
        calls=calls,
    )
    q2, _ = _run(qs.rewrite_questionnaire(q, "luxury"))
    payload = json.loads(calls[-1][-1]["content"].split("\n", 1)[1])
    sent = {it["id"]: it["text"] for it in payload["items"]}
    assert sent["q1"] == "Are you interested?"
    assert sent["q2"] == "What is your budget?"
    assert sent["__intro__"] == "Hi, this is Riya from Raghava."
    # The source snapshot is taken once and never overwritten.
    assert q2["questions"][1]["text_source"] == "What is your budget?"
    assert q2["questions"][1]["text"].startswith("May I ask")
    assert q2["style"] == "luxury"


def test_fallback_lines_keep_wording_and_emit_warnings(monkeypatch):
    reply = json.dumps({
        "items": [
            {"id": "q1", "text": "Hello {name}, interested?"},  # placeholder armor → invalid
            {"id": "q2", "text": "y" * 400},                     # over the 300 question cap → invalid
            {"id": "__intro__", "text": "A fine styled intro."},
            # __outro__ missing from the reply entirely
        ]
    })
    _mock_llm(monkeypatch, reply)
    q, warnings = _run(qs.rewrite_questionnaire(_questionnaire(), "luxury"))
    by_id = {w["id"]: w for w in warnings}
    assert by_id["q1"]["reason"] == "invalid" and by_id["q1"]["kind"] == "question" and by_id["q1"]["index"] == 1
    assert by_id["q2"]["reason"] == "invalid" and by_id["q2"]["index"] == 2
    assert by_id["__outro__"]["reason"] == "missing" and by_id["__outro__"]["kind"] == "outro"
    # Fallen-back lines keep their wording and take no source snapshot.
    assert q["questions"][0]["text"] == "Are you interested?"
    assert "text_source" not in q["questions"][0]
    assert q["outro"] == "Thank you for your time."
    # The good line still applied — and the selection sticks despite fallbacks.
    assert q["intro"] == "A fine styled intro."
    assert q["intro_source"] == "Hi, this is Riya from Raghava."
    assert q["style"] == "luxury"


def test_llm_failure_returns_unchanged_with_single_warning(monkeypatch):
    async def boom(messages, **kwargs):
        raise RuntimeError("pool saturated")

    from app.services import llm_pool

    monkeypatch.setattr(llm_pool.LLMPoolClient, "chat", classmethod(lambda cls, m, **k: boom(m, **k)))
    original = _questionnaire()
    snapshot = json.loads(json.dumps(original))
    q, warnings = _run(qs.rewrite_questionnaire(original, "human"))
    assert q == snapshot
    assert warnings == [{"id": None, "kind": "all", "index": None, "reason": "llm_failed"}]


def test_parse_tolerates_markdown_fence(monkeypatch):
    _mock_llm(monkeypatch, "```json\n" + _styled_reply() + "\n```")
    q, warnings = _run(qs.rewrite_questionnaire(_questionnaire(), "friendly"))
    assert warnings == []
    assert q["questions"][1]["text"].startswith("Could I ask")


def test_blank_intro_is_never_invented(monkeypatch):
    calls = []
    _mock_llm(monkeypatch, _styled_reply(), calls=calls)
    q = _questionnaire()
    q.pop("intro")  # blank intro → the runtime builds its personalized opener
    out, _ = _run(qs.rewrite_questionnaire(q, "human"))
    payload = json.loads(calls[-1][-1]["content"].split("\n", 1)[1])
    assert all(it["id"] != "__intro__" for it in payload["items"])
    assert "intro" not in out and "intro_source" not in out


def test_normalize_style():
    assert qs.normalize_style(" Human ") == "human"
    assert qs.normalize_style("sassy") == "scripted"
    assert qs.normalize_style(None) == "scripted"
    assert qs.normalize_style("scripted") == "scripted"


# ── _coerce_questionnaire round-trip (reload/rerun path) ──


def test_coerce_preserves_style_and_sources():
    from app.services.agent_outbound_context import _coerce_questionnaire

    q = {
        "style": "human",
        "questions": [{"id": "q1", "type": "intent", "text": "Styled?", "text_source": "Original?"}],
        "threshold": 1,
        "intro": "Styled intro.", "intro_source": "Orig intro.",
        "outro": "Styled outro.", "outro_source": "Orig outro.",
    }
    out = _coerce_questionnaire(q)
    assert out["style"] == "human"
    assert out["questions"][0]["text_source"] == "Original?"
    assert out["intro_source"] == "Orig intro."
    assert out["outro_source"] == "Orig outro."
    assert _coerce_questionnaire(out)["style"] == "human"  # survives re-coercion
    # Unknown style degrades to scripted (absent); scripted itself is never stored.
    assert "style" not in _coerce_questionnaire({**q, "style": "sassy"})
    assert "style" not in _coerce_questionnaire({**q, "style": "scripted"})
    # Legacy questionnaires round-trip without any new keys.
    legacy = _coerce_questionnaire({"questions": [{"type": "intent", "text": "Hi?"}], "threshold": 1})
    assert "style" not in legacy and "text_source" not in legacy["questions"][0]


# ── style-matched micro-ack pools ──


def test_ack_pool_style_selection_and_fallback(monkeypatch):
    from app.core.config import settings
    from app.services.apex_micro_acks import ACK_POOLS, STYLE_ACK_POOLS, ack_pool, choose_ack

    assert ack_pool("luxury", "en") == STYLE_ACK_POOLS["luxury"]["en"]
    assert ack_pool(None, "en") == ACK_POOLS["en"]
    assert ack_pool("scripted", "hi") == ACK_POOLS["hi"]
    assert ack_pool("bogus", "te") == ACK_POOLS["te"]
    assert ack_pool("luxury", "ta") == ()  # unsupported language → no pool at all

    monkeypatch.setattr(settings, "APEX_ACK_ENABLED", True)
    monkeypatch.setattr(settings, "APEX_ACK_PROBABILITY", 1.0)
    ack = choose_ack(call_id="call-1", question_idx=2, language="en-IN", delivered_count=1, style="luxury")
    assert ack in STYLE_ACK_POOLS["luxury"]["en"]
    default_ack = choose_ack(call_id="call-1", question_idx=2, language="en-IN", delivered_count=1)
    assert default_ack in ACK_POOLS["en"]


# ── style-rewrite endpoint (POST /bulk-calling/questionnaire/style-rewrite) ──


def _endpoint(monkeypatch, tier="nokvo_apex"):
    import app.api.nokvo_one_voice as api

    async def _noop(db, user):
        return None

    async def _org(db, user):
        from types import SimpleNamespace

        return SimpleNamespace(product_tier=tier)

    monkeypatch.setattr(api, "_require_outbound_enabled", _noop)
    monkeypatch.setattr(api, "_org_for_user", _org)
    return api


def test_style_endpoint_returns_styled_and_warnings(monkeypatch):
    api = _endpoint(monkeypatch)
    _mock_llm(monkeypatch, _styled_reply())
    payload = api.QuestionnaireStyleRewritePayload(
        questionnaire=_questionnaire(), style="human", company_name="Raghava"
    )
    res = _run(api.style_rewrite_bulk_questionnaire(payload, user=object(), _mfa=object(), db=object()))
    assert res["style"] == "human"
    assert res["warnings"] == []
    assert res["questionnaire"]["questions"][0]["text_source"] == "Are you interested?"
    assert res["questionnaire"]["questions"][0]["text"].startswith("Just checking")


def test_style_endpoint_rejects_unknown_style(monkeypatch):
    from fastapi import HTTPException

    api = _endpoint(monkeypatch)
    payload = api.QuestionnaireStyleRewritePayload(questionnaire=_questionnaire(), style="sassy")
    with pytest.raises(HTTPException) as ei:
        _run(api.style_rewrite_bulk_questionnaire(payload, user=object(), _mfa=object(), db=object()))
    assert ei.value.status_code == 422


def test_style_endpoint_soft_degrades_off_tier(monkeypatch):
    api = _endpoint(monkeypatch, tier="nokvo_one")
    called = {"n": 0}

    async def counting(messages, **kwargs):
        called["n"] += 1
        return "{}"

    from app.services import llm_pool

    monkeypatch.setattr(llm_pool.LLMPoolClient, "chat", classmethod(lambda cls, m, **k: counting(m, **k)))
    payload = api.QuestionnaireStyleRewritePayload(questionnaire=_questionnaire(), style="human")
    res = _run(api.style_rewrite_bulk_questionnaire(payload, user=object(), _mfa=object(), db=object()))
    assert called["n"] == 0
    assert res["questionnaire"]["questions"][0]["text"] == "Are you interested?"
    assert res["warnings"] == []
