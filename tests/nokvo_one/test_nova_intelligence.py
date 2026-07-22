"""Nova intelligence upgrade — new-feature knowledge, performance analyst,
rerun-via-chat (confirm), duplicate-into-draft, draft moderation pre-check,
and the panel-open briefing. LLM + Redis + DB are mocked; service-level units
following test_nova_agent.py's harness.
"""
import json
import uuid

import pytest

from app.services import nova_agent_service as nova
from app.services import nova_diagnosis_service as diag
from app.services import nova_session_store as store


# ── shared fakes (mirrors test_nova_agent.py) ────────────────────────────────

class _FakeUser:
    id = "00000000-0000-0000-0000-0000000000aa"
    organization_id = "00000000-0000-0000-0000-000000000001"
    email = "admin@myhome.example"
    role = "admin"


class _FakeTenantRes:
    tenant_id = "t1"
    redis_namespace = "tenant:t1"


class _FakeCampaign:
    def __init__(self, name="December outreach", agent_config=None, status="completed"):
        self.id = uuid.uuid4()
        self.name = name
        self.status = status
        self.agent_config = agent_config or {}
        self.total_count = 100


def _mock_store(monkeypatch):
    sessions: dict[str, dict] = {}

    async def fake_load(ns, sid):
        return json.loads(json.dumps(sessions.get(f"{ns}:{sid}", store.empty_state())))

    async def fake_mutate(ns, sid, fn):
        state = json.loads(json.dumps(sessions.get(f"{ns}:{sid}", store.empty_state())))
        fn(state)
        sessions[f"{ns}:{sid}"] = state
        return state

    monkeypatch.setattr(store, "load", fake_load)
    monkeypatch.setattr(store, "mutate", fake_mutate)
    monkeypatch.setattr(nova.store, "load", fake_load)
    monkeypatch.setattr(nova.store, "mutate", fake_mutate)
    return sessions


def _mock_llm(monkeypatch, responses: list[str]):
    async def fake_llm(messages, tenant_res):
        return responses.pop(0) if responses else "I'm out of ideas."

    monkeypatch.setattr(nova, "_llm", fake_llm)


class _FakeResult:
    def __init__(self, value):
        self._v = value

    def scalars(self):
        return self

    def first(self):
        return self._v


class _FakeDb:
    """Just enough for the org lookup inside _mint_campaign_draft."""

    def __init__(self, value):
        self._value = value

    async def execute(self, *_a, **_k):
        return _FakeResult(self._value)


class _FakeOrg:
    id = "org-1"
    name = "My Home Constructions"
    legal_name = None
    alias_name = "My Home"


# ── platform facts know the new features ─────────────────────────────────────

def test_platform_facts_cover_new_features():
    prompt = nova.build_system_prompt("admin", store.empty_state())
    assert "Busy tab" in prompt
    assert "Duplicate button" in prompt
    assert "disparages/attacks another company" in prompt


def test_new_tools_registered_with_right_flags():
    by_key = {t.key: t for t in nova.TOOLS}
    assert by_key["get_campaign_performance"].admin_only and not by_key["get_campaign_performance"].side_effect
    assert by_key["rerun_campaign"].admin_only and by_key["rerun_campaign"].side_effect
    assert by_key["load_campaign_into_draft"].admin_only and not by_key["load_campaign_into_draft"].side_effect
    assert "get_campaign_performance" in nova._EXECUTORS
    assert "load_campaign_into_draft" in nova._SESSION_EXECUTORS
    assert "rerun_campaign" in nova._MINTERS
    assert "rerun_campaign" in nova.CONFIRM_EXECUTORS
    # Members never see any of them.
    member_keys = {t.key for t in nova.tools_for_role("member")}
    assert not member_keys & {"get_campaign_performance", "rerun_campaign", "load_campaign_into_draft"}


# ── performance recommendations (pure math, server-side) ────────────────────

def _perf_campaign(name, status="completed", **counts):
    full = {"qualified": 0, "not_interested": 0, "busy": 0, "no_pickup": 0, "pending": 0, **counts}
    dialed = full["qualified"] + full["not_interested"] + full["busy"] + full["no_pickup"]
    entry = {"name": name, "status": status, "counts": full}
    if dialed:
        entry["rates_pct"] = {
            "qualified": round(full["qualified"] / dialed * 100, 1),
            "connected": round((dialed - full["no_pickup"]) / dialed * 100, 1),
            "no_pickup": round(full["no_pickup"] / dialed * 100, 1),
            "busy": round(full["busy"] / dialed * 100, 1),
        }
    return entry


def test_recommendations_cover_rerun_busy_best_and_wallet():
    campaigns = [
        _perf_campaign("Winner", qualified=30, not_interested=10, no_pickup=5, busy=3),
        _perf_campaign("Meh", qualified=2, not_interested=40, no_pickup=20),
        _perf_campaign("Stopped", status="cancelled", pending=50, qualified=1, not_interested=9),
    ]
    recs = " | ".join(diag._performance_recommendations(campaigns, {"estimated_minutes_remaining": 12}))
    assert "didn't pick up" in recs and "'Winner'" in recs
    assert "asked to be called back" in recs and "Busy tab" in recs
    assert "best performer is 'Winner'" in recs and "Duplicate" in recs
    assert "'Stopped' is stopped with 50" in recs
    assert "Credits are low" in recs


def test_recommendations_quiet_when_nothing_to_do():
    campaigns = [_perf_campaign("Clean", qualified=5, not_interested=5)]
    # 10 dialed → best-performer fires only with a meaningful sample and >0 rate — it does here.
    recs = diag._performance_recommendations(campaigns, {"estimated_minutes_remaining": 500})
    assert all("didn't pick up" not in r and "called back" not in r and "low" not in r for r in recs)


@pytest.mark.asyncio
async def test_performance_tool_full_loop(monkeypatch):
    _mock_store(monkeypatch)
    _mock_llm(monkeypatch, [
        '```json\n{"tool": "get_campaign_performance", "arguments": {}}\n```',
        "Your December outreach qualified 30% of dialed contacts.",
    ])

    async def fake_perf(db, tenant_res, organization_id):
        return {"campaigns": [{"name": "December outreach", "rates_pct": {"qualified": 30.0}}],
                "recommendations": []}

    monkeypatch.setattr(diag, "build_campaign_performance", fake_perf)
    res = await nova.nova_turn(None, _FakeTenantRes(), _FakeUser(), None, "how did my campaign do?")
    assert res.tool_calls == ["get_campaign_performance"]
    assert "30%" in res.reply


# ── rerun via chat ───────────────────────────────────────────────────────────

def _patch_resolve(monkeypatch, campaign, suggestions=None):
    async def fake_resolve(db, tenant_res, name):
        return campaign, suggestions

    monkeypatch.setattr(nova, "_resolve_campaign_by_name", fake_resolve)


def _patch_summary(monkeypatch, **counts):
    import app.services.campaign_contacts_v2 as v2

    async def fake_summary(db, campaign_id):
        return counts

    monkeypatch.setattr(v2, "summary", fake_summary)


@pytest.mark.asyncio
async def test_rerun_minter_parks_pending_action(monkeypatch):
    sessions = _mock_store(monkeypatch)
    camp = _FakeCampaign()
    _patch_resolve(monkeypatch, camp)
    _patch_summary(monkeypatch, total=100, no_pickup=40, busy=3)
    card, reply = await nova._mint_rerun_campaign(
        None, _FakeTenantRes(), _FakeUser(), {"campaign_name": "december outreach"}, "ns", "sid"
    )
    assert card["type"] == "rerun_preview"
    assert card["campaign_name"] == camp.name
    assert card["buckets"] == ["no_pickup"]
    assert card["counts"]["no_pickup"] == 40
    assert "cost nothing" in reply
    pending = sessions["ns:sid"]["pending_action"]
    assert pending["type"] == "rerun_campaign"
    assert pending["payload"] == {"campaign_id": str(camp.id), "buckets": ["no_pickup"]}


@pytest.mark.asyncio
async def test_rerun_minter_busy_bucket_warns_about_credits(monkeypatch):
    _mock_store(monkeypatch)
    _patch_resolve(monkeypatch, _FakeCampaign())
    _patch_summary(monkeypatch, no_pickup=0, busy=7)
    card, reply = await nova._mint_rerun_campaign(
        None, _FakeTenantRes(), _FakeUser(),
        {"campaign_name": "December outreach", "buckets": ["busy"]}, "ns", "sid",
    )
    assert card["buckets"] == ["busy"]
    assert "consumes Call Credits" in reply


@pytest.mark.asyncio
async def test_rerun_minter_unknown_name_suggests_recents(monkeypatch):
    _mock_store(monkeypatch)
    _patch_resolve(monkeypatch, None, ["December outreach", "Kollur launch"])
    card, reply = await nova._mint_rerun_campaign(
        None, _FakeTenantRes(), _FakeUser(), {"campaign_name": "nope"}, "ns", "sid"
    )
    assert card["type"] == "rerun_not_found"
    assert "December outreach" in reply


@pytest.mark.asyncio
async def test_rerun_minter_nothing_to_dial(monkeypatch):
    _mock_store(monkeypatch)
    _patch_resolve(monkeypatch, _FakeCampaign())
    _patch_summary(monkeypatch, no_pickup=0, busy=0)
    card, reply = await nova._mint_rerun_campaign(
        None, _FakeTenantRes(), _FakeUser(), {"campaign_name": "December outreach"}, "ns", "sid"
    )
    assert card["type"] == "rerun_nothing_to_dial"
    assert "no no pickup" in reply


@pytest.mark.asyncio
async def test_rerun_confirm_executor_dials(monkeypatch):
    from app.services.outbound_campaign_service import OutboundCampaignService
    from app.services.plivo_service import PlivoService

    camp = _FakeCampaign(status="completed")
    seen = {}

    async def fake_get(campaign_id, tr, db):
        return camp

    async def fake_rerun(campaign, db, *, tenant_res, public_base_url, path_prefix, buckets):
        seen["buckets"] = buckets
        campaign.status = "running"
        return campaign

    monkeypatch.setattr(PlivoService, "bulk_calling_enabled", staticmethod(lambda tr: True))
    monkeypatch.setattr(OutboundCampaignService, "get_campaign", staticmethod(fake_get))
    monkeypatch.setattr(OutboundCampaignService, "rerun_bulk_campaign", staticmethod(fake_rerun))
    result, reply = await nova._execute_rerun_campaign(
        None, _FakeTenantRes(), _FakeUser(),
        {"campaign_id": str(camp.id), "buckets": ["no_pickup", "busy"]},
    )
    assert seen["buckets"] == ["no_pickup", "busy"]
    assert result["status"] == "running"
    assert "re-dialing" in reply


@pytest.mark.asyncio
async def test_rerun_confirm_surfaces_slot_conflict_as_reply(monkeypatch):
    from app.services.outbound_campaign_service import OutboundCampaignService
    from app.services.plivo_service import PlivoService

    camp = _FakeCampaign()

    async def fake_get(campaign_id, tr, db):
        return camp

    async def fake_rerun(campaign, db, **kw):
        raise ValueError("'Kollur launch' is still running. Only one campaign can run at a time.")

    monkeypatch.setattr(PlivoService, "bulk_calling_enabled", staticmethod(lambda tr: True))
    monkeypatch.setattr(OutboundCampaignService, "get_campaign", staticmethod(fake_get))
    monkeypatch.setattr(OutboundCampaignService, "rerun_bulk_campaign", staticmethod(fake_rerun))
    result, reply = await nova._execute_rerun_campaign(
        None, _FakeTenantRes(), _FakeUser(), {"campaign_id": str(camp.id)}
    )
    assert "error" in result
    assert "still running" in reply  # friendly reply, no exception → no generic 500


# ── duplicate into draft ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_load_campaign_into_draft_maps_config(monkeypatch):
    sessions = _mock_store(monkeypatch)
    camp = _FakeCampaign(agent_config={
        "agent_prompt": "Premium 3BHK flats in Kokapet",
        "company_name": "My Home",
        "caller_name": "Riya",
        "language": "te",
        "call_window": {"working_days": 6, "hours_per_day": 8, "calls_per_day": 150},
        "questionnaire": {
            "intro": "Hi, quick minute?",
            "outro": "Thanks!",
            "threshold": 2,
            "questions": [
                {"id": "q1", "type": "intent", "text": "Are you looking to buy?", "required": "yes"},
                {"id": "q2", "type": "answer", "text": "Which area?", "desired_answer": "Kokapet"},
            ],
        },
    })
    _patch_resolve(monkeypatch, camp)
    out = await nova._exec_load_campaign_into_draft(
        None, _FakeTenantRes(), _FakeUser(), {"campaign_name": "December outreach"}, ns="ns", sid="sid"
    )
    draft = out["draft"]
    assert draft["name"] == "December outreach (Copy)"
    assert draft["content"].startswith("Premium 3BHK")
    assert draft["language"] == "te"
    assert draft["working_days"] == 6 and draft["calls_per_day"] == 150
    assert len(draft["questionnaire"]["questions"]) == 2
    assert draft["questionnaire"]["intro"] == "Hi, quick minute?"
    assert out["missing"] == []  # complete → Nova can offer finalize immediately
    assert sessions["ns:sid"]["draft"]["name"] == "December outreach (Copy)"
    assert not sessions["ns:sid"]["draft"].get("handed_off")


@pytest.mark.asyncio
async def test_load_campaign_into_draft_unknown_name(monkeypatch):
    _mock_store(monkeypatch)
    _patch_resolve(monkeypatch, None, ["A", "B"])
    out = await nova._exec_load_campaign_into_draft(
        None, _FakeTenantRes(), _FakeUser(), {"campaign_name": "nope"}, ns="ns", sid="sid"
    )
    assert "error" in out and out["recent_campaigns"] == ["A", "B"]


# ── draft moderation pre-check at finalize ───────────────────────────────────

_COMPLETE_DRAFT = {
    "name": "Kokapet launch",
    "content": "Premium flats",
    "questionnaire": {"intro": "Hi", "outro": "Bye", "threshold": 1,
                      "questions": [{"id": "q1", "type": "intent", "text": "Interested?", "required": "yes"}]},
}


def _prime_draft(sessions, draft):
    state = store.empty_state()
    state["draft"] = json.loads(json.dumps(draft))
    sessions["ns:sid"] = state


def _patch_moderation(monkeypatch, verdict, calls=None):
    import app.services.campaign_moderation_service as cms

    async def fake_moderate(org, *, campaign_name, agent_config, timeout_ms=10000):
        if calls is not None:
            calls.append({"campaign_name": campaign_name, "agent_config": agent_config})
        return verdict

    monkeypatch.setattr(cms, "moderate_campaign_content", fake_moderate)


def _patch_estimate(monkeypatch):
    async def fake_estimate(db, organization_id, draft):
        return {"per_call_credits": 2.0, "daily_spend": 400.0, "balance": 5000.0, "balance_warning": False}

    monkeypatch.setattr(nova, "draft_estimate", fake_estimate)


@pytest.mark.asyncio
async def test_finalize_blocked_when_flagged_in_enforce(monkeypatch):
    from app.services.campaign_moderation_service import ModerationVerdict

    sessions = _mock_store(monkeypatch)
    _prime_draft(sessions, _COMPLETE_DRAFT)
    monkeypatch.setattr(nova.settings, "CAMPAIGN_CONTENT_MODERATION", "enforce")
    _patch_estimate(monkeypatch)
    calls = []
    _patch_moderation(monkeypatch, ModerationVerdict(
        allowed=False, category="third_party_disparagement", reason="the intro attacks Aparna"
    ), calls)
    card, reply = await nova._mint_campaign_draft(
        _FakeDb(_FakeOrg()), _FakeTenantRes(), _FakeUser(), {}, "ns", "sid"
    )
    assert card["type"] == "draft_flagged"
    assert "attacks Aparna" in reply
    assert not sessions["ns:sid"]["draft"].get("handed_off")  # draft stays editable
    # Moderation saw the draft's full text surface, questionnaire included.
    assert calls and calls[0]["agent_config"]["questionnaire"]["questions"]


@pytest.mark.asyncio
async def test_finalize_impersonation_hands_off_in_enforce(monkeypatch):
    """Even in enforce mode, a possible-impersonation flag is advisory only —
    Nova hands the draft off (the dealership / JV-partner false-positive case)
    with a gentle note rather than blocking."""
    from app.services.campaign_moderation_service import ModerationVerdict

    sessions = _mock_store(monkeypatch)
    _prime_draft(sessions, _COMPLETE_DRAFT)
    monkeypatch.setattr(nova.settings, "CAMPAIGN_CONTENT_MODERATION", "enforce")
    _patch_estimate(monkeypatch)
    _patch_moderation(monkeypatch, ModerationVerdict(
        allowed=False, category="impersonation", reason="names Toyota"
    ))
    card, reply = await nova._mint_campaign_draft(
        _FakeDb(_FakeOrg()), _FakeTenantRes(), _FakeUser(), {}, "ns", "sid"
    )
    assert card["type"] == "campaign_draft"  # handed off, not draft_flagged
    assert "names Toyota" in reply
    assert sessions["ns:sid"]["draft"]["handed_off"] is True


@pytest.mark.asyncio
async def test_finalize_warn_mode_hands_off_with_warning(monkeypatch):
    from app.services.campaign_moderation_service import ModerationVerdict

    sessions = _mock_store(monkeypatch)
    _prime_draft(sessions, _COMPLETE_DRAFT)
    monkeypatch.setattr(nova.settings, "CAMPAIGN_CONTENT_MODERATION", "warn")
    _patch_estimate(monkeypatch)
    _patch_moderation(monkeypatch, ModerationVerdict(allowed=False, category="impersonation", reason="claims to be Aparna"))
    card, reply = await nova._mint_campaign_draft(
        _FakeDb(_FakeOrg()), _FakeTenantRes(), _FakeUser(), {}, "ns", "sid"
    )
    assert card["type"] == "campaign_draft"
    assert "claims to be Aparna" in reply and "reword" in reply
    assert sessions["ns:sid"]["draft"]["handed_off"] is True


@pytest.mark.asyncio
async def test_finalize_off_mode_skips_moderation(monkeypatch):
    sessions = _mock_store(monkeypatch)
    _prime_draft(sessions, _COMPLETE_DRAFT)
    monkeypatch.setattr(nova.settings, "CAMPAIGN_CONTENT_MODERATION", "off")
    _patch_estimate(monkeypatch)
    calls = []
    _patch_moderation(monkeypatch, None, calls)  # would explode if called (returns None)
    card, reply = await nova._mint_campaign_draft(
        _FakeDb(_FakeOrg()), _FakeTenantRes(), _FakeUser(), {}, "ns", "sid"
    )
    assert card["type"] == "campaign_draft"
    assert calls == []


@pytest.mark.asyncio
async def test_finalize_clean_verdict_hands_off_normally(monkeypatch):
    from app.services.campaign_moderation_service import ModerationVerdict

    sessions = _mock_store(monkeypatch)
    _prime_draft(sessions, _COMPLETE_DRAFT)
    monkeypatch.setattr(nova.settings, "CAMPAIGN_CONTENT_MODERATION", "enforce")
    _patch_estimate(monkeypatch)
    _patch_moderation(monkeypatch, ModerationVerdict(allowed=True))
    card, reply = await nova._mint_campaign_draft(
        _FakeDb(_FakeOrg()), _FakeTenantRes(), _FakeUser(), {}, "ns", "sid"
    )
    assert card["type"] == "campaign_draft"
    assert "ready to go" in reply
    assert sessions["ns:sid"]["draft"]["handed_off"] is True


# ── panel-open briefing ──────────────────────────────────────────────────────

def _patch_briefing_blocks(monkeypatch, campaigns, wallet=None):
    async def fake_stats(db, tenant_id, limit):
        return campaigns

    async def fake_wallet(db, org_id):
        return wallet or {"credits_remaining": 5000, "estimated_minutes_remaining": 500}

    monkeypatch.setattr(diag, "_campaign_stats", fake_stats)
    monkeypatch.setattr(diag, "_wallet", fake_wallet)


@pytest.mark.asyncio
async def test_briefing_running_campaign_and_busy(monkeypatch):
    _patch_briefing_blocks(monkeypatch, [
        {"name": "Live one", "status": "running", "total": 100,
         "counts": {"qualified": 8, "not_interested": 12, "busy": 4, "no_pickup": 16, "pending": 60}},
    ])
    out = await diag.build_briefing(None, _FakeTenantRes(), "org-1", "admin")
    joined = " ".join(out["highlights"])
    assert "'Live one' is dialing — 40 of 100" in joined
    assert "8 qualified" in joined
    assert "Busy tab" in joined


@pytest.mark.asyncio
async def test_briefing_completed_and_low_credits(monkeypatch):
    _patch_briefing_blocks(
        monkeypatch,
        [{"name": "Done one", "status": "completed", "total": 50,
          "counts": {"qualified": 5, "not_interested": 30, "busy": 0, "no_pickup": 15, "pending": 0}}],
        wallet={"credits_remaining": 40, "estimated_minutes_remaining": 6},
    )
    out = await diag.build_briefing(None, _FakeTenantRes(), "org-1", "admin")
    joined = " ".join(out["highlights"])
    assert "'Done one' finished: 5 qualified" in joined
    assert "Credits are low" in joined


@pytest.mark.asyncio
async def test_briefing_member_gets_nothing(monkeypatch):
    _patch_briefing_blocks(monkeypatch, [{"name": "X", "status": "running", "counts": {"pending": 1}}])
    out = await diag.build_briefing(None, _FakeTenantRes(), "org-1", "member")
    assert out == {"highlights": []}
