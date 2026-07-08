"""Nova campaign drafting — slot merge, missing[], estimate, finalize, handoff.

Pure-service units: DB, Redis, and the LLM are mocked.
"""
import json
import uuid

import pytest

from app.services import nova_agent_service as nova
from app.services import nova_session_store as store


class _FakeUser:
    id = uuid.uuid4()
    organization_id = uuid.uuid4()
    email = "admin@example.com"
    role = "admin"


class _FakeMember(_FakeUser):
    role = "member"


class _FakeTenantRes:
    tenant_id = "t1"
    redis_namespace = "tenant:t1"


def _mock_store(monkeypatch):
    sessions: dict[str, dict] = {}

    async def fake_load(ns, sid):
        return json.loads(json.dumps(sessions.get(f"{ns}:{sid}", store.empty_state())))

    async def fake_mutate(ns, sid, fn):
        state = json.loads(json.dumps(sessions.get(f"{ns}:{sid}", store.empty_state())))
        fn(state)
        sessions[f"{ns}:{sid}"] = state
        return state

    monkeypatch.setattr(nova.store, "load", fake_load)
    monkeypatch.setattr(nova.store, "mutate", fake_mutate)
    monkeypatch.setattr(
        nova.store.AgentSessionStore, "namespace", staticmethod(lambda tr: tr.redis_namespace)
    )
    return sessions


def _mock_estimate(monkeypatch, *, balance=100000.0):
    async def fake_est(db, org_id, draft):
        questions = ((draft.get("questionnaire") or {}).get("questions")) or []
        sec = 12 + sum(15 if q.get("type") == "answer" else 11 for q in questions)
        per_call = 1.5 + (10 - 1.5) / 60 * sec
        daily = per_call * int(draft.get("calls_per_day") or 200)
        return {"est_call_seconds": sec, "per_call_credits": round(per_call, 2),
                "daily_spend": round(daily, 2), "balance": balance,
                "balance_warning": daily > balance}

    monkeypatch.setattr(nova, "draft_estimate", fake_est)


_QUESTIONNAIRE = {
    "intro": "Hi, this is Riya from Acme Estates.",
    "outro": "Thanks for your time!",
    "threshold": 2,
    "questions": [
        {"type": "intent", "text": "Are you looking to buy a flat this year?"},
        {"type": "answer", "text": "What's your budget?", "desired_answer": "above 50 lakhs"},
    ],
}


# ── slot merge + missing[] ────────────────────────────────────────────────────

def test_merge_normalizes_questionnaire_via_canonical_coercer():
    draft = {}
    nova.merge_draft_fields(draft, {"questionnaire": dict(_QUESTIONNAIRE)})
    q = draft["questionnaire"]
    assert q["intro"].startswith("Hi, this is Riya")
    assert len(q["questions"]) == 2
    # The canonical coercer stamps ids and forces intent desired_answer to "".
    assert all(item.get("id") for item in q["questions"])
    assert q["questions"][0]["desired_answer"] == ""
    assert q["threshold"] == 2


def test_missing_order_matches_slot_order():
    assert nova.draft_missing({})[0].startswith("the offer/pitch")
    draft = {"content": "Great flats in Hyderabad."}
    assert nova.draft_missing(draft)[0].startswith("the qualifying questions")
    nova.merge_draft_fields(draft, {"questionnaire": dict(_QUESTIONNAIRE)})
    assert nova.draft_missing(draft) == ["a campaign name"]
    draft["name"] = "July outreach"
    assert nova.draft_missing(draft) == []  # schedule fields have defaults


def test_intro_required_for_questionnaire_slot():
    draft = {"content": "x", "name": "y",
             "questionnaire": {"questions": [{"id": "a", "type": "intent", "text": "Q?", "desired_answer": ""}]}}
    # No intro → the questionnaire slot still counts as missing.
    assert any("qualifying questions" in m for m in nova.draft_missing(draft))


# ── set_campaign_fields through the loop ──────────────────────────────────────

@pytest.mark.asyncio
async def test_set_fields_merges_and_reports_next(monkeypatch):
    sessions = _mock_store(monkeypatch)
    replies = [
        '```json\n{"tool": "set_campaign_fields", "arguments": {"content": "Premium 3BHK flats at Skyline Heights, Kokapet, from 1.2Cr."}}\n```',
        "Noted! What should the agent ask to qualify a lead?",
    ]

    async def fake_llm(messages, tenant_res):
        return replies.pop(0)

    monkeypatch.setattr(nova, "_llm", fake_llm)
    res = await nova.nova_turn(None, _FakeTenantRes(), _FakeUser(), None, "we sell 3BHKs at Skyline Heights")
    assert res.tool_calls == ["set_campaign_fields"]
    # Sessions are tenant+USER namespaced.
    state = sessions[f"tenant:t1:u{_FakeUser.id}:{res.session_id}"]
    assert "Skyline Heights" in state["draft"]["content"]


@pytest.mark.asyncio
async def test_member_cannot_use_campaign_tools(monkeypatch):
    _mock_store(monkeypatch)
    replies = [
        '```json\n{"tool": "set_campaign_fields", "arguments": {"content": "hack"}}\n```',
        "I can't create campaigns for members.",
    ]

    async def fake_llm(messages, tenant_res):
        return replies.pop(0)

    monkeypatch.setattr(nova, "_llm", fake_llm)
    res = await nova.nova_turn(None, _FakeTenantRes(), _FakeMember(), None, "make a campaign")
    assert res.tool_calls == []  # unknown-tool correction path, never executed


@pytest.mark.asyncio
async def test_edit_invalidates_pending_action(monkeypatch):
    sessions = _mock_store(monkeypatch)
    sid = "s1"
    sessions[f"tenant:t1:{sid}"] = {
        **store.empty_state(),
        "draft": {"content": "x"},
        "pending_action": {"action_id": "abc", "type": "create_ticket", "payload": {}},
    }
    out = await nova._exec_set_campaign_fields(
        None, _FakeTenantRes(), _FakeUser(), {"name": "New name"}, ns="tenant:t1", sid=sid
    )
    assert sessions[f"tenant:t1:{sid}"]["pending_action"] is None
    assert out["draft"]["name"] == "New name"


# ── finalize + one-way handoff ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_finalize_refuses_incomplete_draft(monkeypatch):
    _mock_store(monkeypatch)
    _mock_estimate(monkeypatch)
    card, reply = await nova._mint_campaign_draft(
        None, _FakeTenantRes(), _FakeUser(), {}, "tenant:t1", "s-empty"
    )
    assert card["type"] == "draft_incomplete"
    assert "still need" in reply


@pytest.mark.asyncio
async def test_finalize_emits_card_and_freezes_draft(monkeypatch):
    sessions = _mock_store(monkeypatch)
    _mock_estimate(monkeypatch)
    sid = "s2"
    draft = {"content": "pitch", "name": "July blast"}
    nova.merge_draft_fields(draft, {"questionnaire": dict(_QUESTIONNAIRE)})
    sessions[f"tenant:t1:{sid}"] = {**store.empty_state(), "draft": draft}

    card, reply = await nova._mint_campaign_draft(
        None, _FakeTenantRes(), _FakeUser(), {}, "tenant:t1", sid
    )
    assert card["type"] == "campaign_draft"
    assert card["draft"]["name"] == "July blast"
    assert card["draft"]["caller_name"] == "Riya"       # defaults filled
    assert card["draft"]["working_days"] == 5
    assert card["estimate"]["per_call_credits"] > 1.5
    assert "handed_off" not in card["draft"]            # never leaks to the form
    assert sessions[f"tenant:t1:{sid}"]["draft"]["handed_off"] is True
    assert "won't see those edits" in reply


@pytest.mark.asyncio
async def test_handed_off_draft_edits_start_fresh(monkeypatch):
    sessions = _mock_store(monkeypatch)
    sid = "s3"
    sessions[f"tenant:t1:{sid}"] = {
        **store.empty_state(),
        "draft": {"content": "old pitch", "name": "Old", "handed_off": True},
    }
    out = await nova._exec_set_campaign_fields(
        None, _FakeTenantRes(), _FakeUser(), {"content": "new pitch"}, ns="tenant:t1", sid=sid
    )
    # Old fields gone — a handed-off draft is frozen; edits begin a NEW draft.
    assert out["draft"].get("name", "") in ("", None) or "name" not in out["draft"]
    assert out["draft"]["content"] == "new pitch"


@pytest.mark.asyncio
async def test_balance_warning_reaches_reply(monkeypatch):
    sessions = _mock_store(monkeypatch)
    _mock_estimate(monkeypatch, balance=10.0)
    sid = "s4"
    draft = {"content": "pitch", "name": "Big blast", "calls_per_day": 500}
    nova.merge_draft_fields(draft, {"questionnaire": dict(_QUESTIONNAIRE)})
    sessions[f"tenant:t1:{sid}"] = {**store.empty_state(), "draft": draft}
    card, reply = await nova._mint_campaign_draft(
        None, _FakeTenantRes(), _FakeUser(), {}, "tenant:t1", sid
    )
    assert card["estimate"]["balance_warning"] is True
    assert "top up" in reply
