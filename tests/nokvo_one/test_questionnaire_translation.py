"""Pre-translation of an APEX questionnaire at campaign creation.

Fills text_i18n (per question) + outro_i18n into en/hi/te via one LLM call (mocked).
Best-effort: an LLM failure floors every line to {"en": authored} so verbatim
delivery always fires; re-runs are idempotent.
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


def test_llm_failure_floors_to_authored_text(monkeypatch):
    async def boom(messages, **kwargs):
        raise RuntimeError("pool saturated")

    from app.services import llm_pool

    monkeypatch.setattr(llm_pool.LLMPoolClient, "chat", classmethod(lambda cls, m, **k: boom(m, **k)))
    q = _run(qt.translate_questionnaire(_questionnaire()))
    # Floored, not dropped: next_verbatim_question requires a non-empty text_i18n,
    # so a failed translation still gets {"en": authored} (the questionnaire-loop fix).
    assert q["questions"][0]["text_i18n"] == {"en": "Are you interested?"}
    assert q["questions"][1]["text_i18n"] == {"en": "What is your budget?"}
    assert q["outro_i18n"] == {"en": "Thank you for your time."}


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


# ── intro (admin-authored opener) i18n ──


def _questionnaire_with_intro():
    q = _questionnaire()
    q["intro"] = "Hi, this is Riya from Raghava."
    return q


def test_fills_intro_i18n(monkeypatch):
    _mock_llm(monkeypatch, _good_reply(["q1", "q2", "__intro__", "__outro__"]))
    q = _run(qt.translate_questionnaire(_questionnaire_with_intro()))
    assert q["intro_i18n"] == {"en": "EN __intro__", "hi": "HI __intro__", "te": "TE __intro__"}


def test_intro_floors_on_llm_failure(monkeypatch):
    async def boom(messages, **kwargs):
        raise RuntimeError("pool saturated")

    from app.services import llm_pool

    monkeypatch.setattr(llm_pool.LLMPoolClient, "chat", classmethod(lambda cls, m, **k: boom(m, **k)))
    q = _run(qt.translate_questionnaire(_questionnaire_with_intro()))
    assert q["intro_i18n"] == {"en": "Hi, this is Riya from Raghava."}


def test_partial_hand_edit_is_preserved(monkeypatch):
    # An admin hand-edited the Hindi line and cleared Telugu → the row re-enters
    # translation (incomplete), but the merge fills ONLY the blanks: the edited
    # hi (and the existing en) survive verbatim, te comes from the model.
    _mock_llm(monkeypatch, _good_reply(["q1", "q2", "__outro__"]))
    q = _questionnaire()
    q["questions"][0]["text_i18n"] = {"en": "Are you interested?", "hi": "क्या आप इच्छुक हैं?", "te": ""}
    out = _run(qt.translate_questionnaire(q))
    assert out["questions"][0]["text_i18n"] == {
        "en": "Are you interested?",
        "hi": "क्या आप इच्छुक हैं?",
        "te": "TE q1",
    }


def test_coerce_round_trips_intro_i18n():
    # intro_i18n must survive _coerce_questionnaire (the reload/rerun path) like
    # text_i18n/outro_i18n do — otherwise the per-language opener dies on reload.
    from app.services.agent_outbound_context import _coerce_questionnaire

    q = {
        "questions": [{"type": "intent", "text": "Interested?", "text_i18n": {"en": "a", "hi": "b", "te": "c"}}],
        "threshold": 1,
        "intro": "Hi.", "intro_i18n": {"en": "Hi.", "hi": "नमस्ते.", "te": "హాయ్."},
        "outro": "Bye.", "outro_i18n": {"en": "Bye.", "hi": "अलविदा.", "te": "వీడ్కోలు."},
    }
    out = _coerce_questionnaire(q)
    assert out["intro_i18n"] == q["intro_i18n"]
    assert out["outro_i18n"] == q["outro_i18n"]
    assert _coerce_questionnaire(out)["intro_i18n"] == q["intro_i18n"]


# ── translate-preview endpoint (POST /bulk-calling/questionnaire/translate) ──


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


def test_translate_endpoint_returns_i18n(monkeypatch):
    api = _endpoint(monkeypatch)
    _mock_llm(monkeypatch, _good_reply(["q1", "__outro__"]))
    payload = api.QuestionnaireTranslatePayload(
        questionnaire={
            "questions": [{"id": "q1", "type": "intent", "text": "Are you interested?"}],
            "threshold": 1,
            "outro": "Thanks for your time.",
        }
    )
    res = _run(api.translate_bulk_questionnaire(payload, user=object(), _mfa=object(), db=object()))
    q = res["questionnaire"]
    assert q["questions"][0]["text_i18n"]["te"] == "TE q1"
    assert q["outro_i18n"]["hi"] == "HI __outro__"


def test_translate_endpoint_skips_complete_entries(monkeypatch):
    api = _endpoint(monkeypatch)
    calls = {"n": 0}

    async def counting(messages, **kwargs):
        calls["n"] += 1
        return "{}"

    from app.services import llm_pool

    monkeypatch.setattr(llm_pool.LLMPoolClient, "chat", classmethod(lambda cls, m, **k: counting(m, **k)))
    done = {"en": "a", "hi": "b", "te": "c"}
    payload = api.QuestionnaireTranslatePayload(
        questionnaire={
            "questions": [{"id": "q1", "type": "intent", "text": "Interested?", "text_i18n": done}],
            "threshold": 1,
        }
    )
    res = _run(api.translate_bulk_questionnaire(payload, user=object(), _mfa=object(), db=object()))
    assert calls["n"] == 0  # nothing to translate → no LLM call
    assert res["questionnaire"]["questions"][0]["text_i18n"] == done


def test_translate_endpoint_rejects_bad_input_with_422(monkeypatch):
    from fastapi import HTTPException

    api = _endpoint(monkeypatch)
    # Malformed: an answer question with neither an expected answer nor bands.
    bad = api.QuestionnaireTranslatePayload(
        questionnaire={"questions": [{"type": "answer", "text": "Budget?", "desired_answer": ""}], "threshold": 1}
    )
    with pytest.raises(HTTPException) as ei:
        _run(api.translate_bulk_questionnaire(bad, user=object(), _mfa=object(), db=object()))
    assert ei.value.status_code == 422
    # Empty: no questions at all.
    empty = api.QuestionnaireTranslatePayload(questionnaire={"questions": []})
    with pytest.raises(HTTPException) as ei:
        _run(api.translate_bulk_questionnaire(empty, user=object(), _mfa=object(), db=object()))
    assert ei.value.status_code == 422


# ── drop_stale_i18n (campaign EDIT) ──


def test_drop_stale_i18n_only_for_changed_lines():
    old = {
        "questions": [
            {"id": "q1", "text": "Are you interested?"},
            {"id": "q2", "text": "What is your budget?"},
        ],
        "intro": "Hi there.",
        "outro": "Thanks, bye.",
    }
    full = {"en": "a", "hi": "b", "te": "c"}
    new = {
        "questions": [
            {"id": "q1", "text": "Are you interested?", "text_i18n": dict(full)},   # unchanged
            {"id": "q2", "text": "What's your budget range?", "text_i18n": dict(full)},  # EDITED
            {"id": "q3", "text": "New question?", "text_i18n": dict(full)},         # new id → kept
        ],
        "intro": "Hi there.", "intro_i18n": dict(full),        # unchanged
        "outro": "Goodbye now.", "outro_i18n": dict(full),     # EDITED
    }
    qt.drop_stale_i18n(new, old)
    assert new["questions"][0]["text_i18n"] == full          # untouched line keeps (hand-)edits
    assert "text_i18n" not in new["questions"][1]            # edited text → stale i18n dropped
    assert new["questions"][2]["text_i18n"] == full          # brand-new question keeps provided i18n
    assert new["intro_i18n"] == full
    assert "outro_i18n" not in new


# ── campaign edit endpoint (PATCH /bulk-calling/campaigns/{id}) ──


def _fake_campaign(cfg):
    from types import SimpleNamespace
    import uuid as _uuid

    return SimpleNamespace(
        id=_uuid.uuid4(), name="Old name", status="running", from_number=None,
        total_count=0, answered_count=0, failed_count=0, contacts=None,
        agent_config=cfg, doc_blob_path=None,
        created_at=None, started_at=None, completed_at=None,
    )


def _update_endpoint(monkeypatch, campaign):
    import app.api.nokvo_one_voice as api
    from app.services.outbound_campaign_service import OutboundCampaignService

    async def _noop(db, user):
        return None

    async def _org(db, user):
        from types import SimpleNamespace

        return SimpleNamespace(product_tier="nokvo_apex")

    async def _tenant(db, user):
        return object()

    async def _get(cid, tr, db):
        return campaign

    monkeypatch.setattr(api, "_require_outbound_enabled", _noop)
    monkeypatch.setattr(api, "_org_for_user", _org)
    monkeypatch.setattr(api, "_tenant_for_user", _tenant)
    monkeypatch.setattr(OutboundCampaignService, "get_campaign", staticmethod(_get))
    return api


class _EditFakeDB:
    def add(self, *_a, **_k): pass
    async def commit(self): pass
    async def refresh(self, *_a, **_k): pass


def test_update_endpoint_edits_config_and_keeps_runtime_counter(monkeypatch):
    _mock_llm(monkeypatch, _good_reply(["q1", "__intro__", "__outro__"]))
    cfg = {
        "deterministic": True,
        "questionnaire": {
            "questions": [{"id": "q1", "type": "intent", "text": "Interested?"}],
            "threshold": 1, "intro": "Old intro.", "outro": "Old outro.",
        },
        "call_window": {
            "working_days": 2, "hours_per_day": 10,
            "timezone": "Asia/Kolkata", "window_local": "09:00-19:00",
            "calls_per_day": 200,
            "dialed_today": {"date": "2026-07-08", "count": 37},   # runtime counter
        },
        "company_name": "Old Co",
    }
    campaign = _fake_campaign(cfg)
    api = _update_endpoint(monkeypatch, campaign)
    payload = api.BulkCampaignUpdatePayload(
        name="New name", company_name="New Co", working_days=5,
        questionnaire={
            "questions": [{"id": "q1", "type": "intent", "text": "Are you interested?"}],
            "threshold": 1, "intro": "New intro.", "outro": "Old outro.",
        },
    )
    res = _run(api.update_bulk_calling_campaign(
        campaign.id, payload, user=object(), _mfa=object(), db=_EditFakeDB()
    ))
    assert res["name"] == "New name"
    new_cfg = campaign.agent_config
    assert new_cfg["company_name"] == "New Co"
    w = new_cfg["call_window"]
    assert w["working_days"] == 5
    assert w["calls_per_day"] == 200                          # untouched field survives
    assert w["dialed_today"] == {"date": "2026-07-08", "count": 37}  # runtime counter survives
    # Question text changed → re-translated (mock fills en/hi/te).
    assert new_cfg["questionnaire"]["questions"][0]["text_i18n"]["te"] == "TE q1"
    assert new_cfg["questionnaire"]["intro_i18n"]["hi"] == "HI __intro__"


def test_update_endpoint_rejects_dropping_the_questionnaire(monkeypatch):
    import pytest as _pytest
    from fastapi import HTTPException

    cfg = {"deterministic": True, "questionnaire": {"questions": [{"id": "q1", "type": "intent", "text": "Hi?"}], "threshold": 1}}
    campaign = _fake_campaign(cfg)
    api = _update_endpoint(monkeypatch, campaign)
    payload = api.BulkCampaignUpdatePayload(questionnaire={"questions": []})
    with _pytest.raises(HTTPException) as ei:
        _run(api.update_bulk_calling_campaign(
            campaign.id, payload, user=object(), _mfa=object(), db=_EditFakeDB()
        ))
    assert ei.value.status_code == 422
