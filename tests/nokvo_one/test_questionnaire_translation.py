"""Pre-translation of an APEX questionnaire at campaign creation.

Fills text_i18n (per question) + outro_i18n into en/hi/te via one LLM call (mocked).
Best-effort: an LLM failure leaves the authored text; re-runs are idempotent.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import app.services.questionnaire_translation as qt


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _questionnaire():
    return {
        "questions": [
            {"id": "q1", "type": "intent", "text": "Are you interested?"},
            {"id": "q2", "type": "answer", "text": "What is your budget?"},
        ],
        "threshold": 1,
        "outro": "Thank you for your time.",
    }


def _mock_llm(monkeypatch, reply):
    async def fake_chat(messages, **kwargs):
        return reply

    from app.services import llm_pool

    monkeypatch.setattr(llm_pool.LLMPoolClient, "chat", classmethod(lambda cls, m, **k: fake_chat(m, **k)))


def _good_reply(ids):
    return json.dumps({
        "items": [
            {"id": i, "en": f"EN {i}", "hi": f"HI {i}", "te": f"TE {i}"} for i in ids
        ]
    })


def test_fills_i18n_for_questions_and_outro(monkeypatch):
    _mock_llm(monkeypatch, _good_reply(["q1", "q2", "__outro__"]))
    q = _run(qt.translate_questionnaire(_questionnaire()))
    assert q["questions"][0]["text_i18n"] == {"en": "EN q1", "hi": "HI q1", "te": "TE q1"}
    assert q["questions"][1]["text_i18n"]["te"] == "TE q2"
    assert q["outro_i18n"] == {"en": "EN __outro__", "hi": "HI __outro__", "te": "TE __outro__"}


def test_llm_failure_leaves_authored_text(monkeypatch):
    async def boom(messages, **kwargs):
        raise RuntimeError("pool saturated")

    from app.services import llm_pool

    monkeypatch.setattr(llm_pool.LLMPoolClient, "chat", classmethod(lambda cls, m, **k: boom(m, **k)))
    q = _run(qt.translate_questionnaire(_questionnaire()))
    assert "text_i18n" not in q["questions"][0]   # fell back — call-time uses `text`
    assert "outro_i18n" not in q


def test_idempotent_skips_already_translated(monkeypatch):
    calls = {"n": 0}

    async def counting(messages, **kwargs):
        calls["n"] += 1
        return _good_reply(["q2"])  # only q2 should be requested the 2nd time

    from app.services import llm_pool

    monkeypatch.setattr(llm_pool.LLMPoolClient, "chat", classmethod(lambda cls, m, **k: counting(m, **k)))

    q = _questionnaire()
    q["questions"][0]["text_i18n"] = {"en": "x", "hi": "y", "te": "z"}  # already done
    q.pop("outro")  # only q2 remains untranslated
    out = _run(qt.translate_questionnaire(q))
    assert calls["n"] == 1
    assert out["questions"][0]["text_i18n"] == {"en": "x", "hi": "y", "te": "z"}  # untouched
    assert out["questions"][1]["text_i18n"]["en"] == "EN q2"


def test_nothing_to_translate_makes_no_call(monkeypatch):
    called = {"n": 0}

    async def counting(messages, **kwargs):
        called["n"] += 1
        return "{}"

    from app.services import llm_pool

    monkeypatch.setattr(llm_pool.LLMPoolClient, "chat", classmethod(lambda cls, m, **k: counting(m, **k)))
    # A questionnaire whose only question already has i18n and no outro.
    q = {"questions": [{"id": "q1", "type": "intent", "text": "Hi", "text_i18n": {"en": "a", "hi": "b", "te": "c"}}], "threshold": 1}
    _run(qt.translate_questionnaire(q))
    assert called["n"] == 0


def test_parse_tolerates_markdown_fence(monkeypatch):
    fenced = "```json\n" + _good_reply(["q1", "q2", "__outro__"]) + "\n```"
    _mock_llm(monkeypatch, fenced)
    q = _run(qt.translate_questionnaire(_questionnaire()))
    assert q["questions"][0]["text_i18n"]["en"] == "EN q1"
