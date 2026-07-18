"""Busy / call-me-later outcome bucket + bucket-targeted re-run.

A connected caller who says "I'm busy, call me later" is stamped
``callback_requested`` post-call (deterministic phrase scan over CALLER lines,
opt-out override) and lands in the "busy" bucket instead of Not Interested.
The campaign card's Call didn't pick up / Call busy / Call both buttons drive
``rerun_bulk_campaign(buckets=...)``, which re-arms exactly the selected
groups (legacy blob path exercised here; the V2 path is the same buckets via
``campaign_contacts_v2.rearm_unreached``).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services.lead_score_service import detect_callback_request


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _turns(*user_lines: str):
    return [{"query": line, "answer": "ok"} for line in user_lines]


# ── detection ──


def test_detects_busy_and_call_back_phrases():
    positives = [
        "I'm busy right now",
        "am very busy, in a meeting actually",
        "can you call me later",
        "call me back tomorrow please",
        "this is not a good time",
        "call me after 6",
        "अभी busy हूँ, बाद में call करना",
        "मैं मीटिंग में हूँ",
        "నేను బిజీగా ఉన్నాను, తర్వాత చేయండి",
    ]
    for line in positives:
        assert detect_callback_request(_turns(line)), line


def test_optout_in_same_line_wins():
    negatives = [
        "not interested, don't call me later",
        "stop calling me",
        "wrong number, call someone else later",
        "मुझे दिलचस्पी नहीं है, बाद में भी मत करना",
        "వద్దు, తర్వాత కూడా చేయకండి",
    ]
    for line in negatives:
        assert not detect_callback_request(_turns(line)), line


def test_plain_answers_do_not_trigger():
    assert not detect_callback_request(_turns("my budget is around 50 lakhs", "yes I am the owner"))
    assert not detect_callback_request(_turns())
    assert not detect_callback_request(None)


def test_agent_lines_are_ignored():
    # The busy phrase must come from the CALLER (query), not the agent (answer).
    turns = [{"query": "hello", "answer": "should I call you later instead?"}]
    assert not detect_callback_request(turns)


# ── the live BUSY dealbreaker (busy line → busy close → hang up) ──


def test_is_callback_line_single_utterance():
    from app.services.lead_score_service import is_callback_line

    assert is_callback_line("sorry I'm busy right now, call me later")
    assert not is_callback_line("not interested, don't call me later")
    assert not is_callback_line("my budget is 80 lakhs")
    assert not is_callback_line("")
    assert not is_callback_line(None)


def test_busy_outro_is_per_language_native_script():
    from app.services.nokvo_one_voice_stream_service import _BUSY_OUTROS, _busy_outro

    assert _busy_outro("en-IN") == _BUSY_OUTROS["en"]
    assert _busy_outro("hi") == _BUSY_OUTROS["hi"]
    assert _busy_outro("te-IN") == _BUSY_OUTROS["te"]
    assert _busy_outro("ta") == _BUSY_OUTROS["en"]  # unsupported → English
    assert "बाद में" in _BUSY_OUTROS["hi"] and "తర్వాత" in _BUSY_OUTROS["te"]


def test_busy_cut_is_wired_before_the_other_lanes():
    # Source pin (same style as the lock-campaign guard test): the deterministic
    # close block must check is_callback_line FIRST and close via the busy outro
    # — if this wiring is ever dropped, busy callers get pushed to the next
    # question again.
    import inspect

    # The dispatcher body lives in voice_stream/text_turn.py since the
    # voice-modularization (the class keeps a delegating wrapper).
    from app.services.voice_stream.text_turn import _run_text_turn

    src = inspect.getsource(_run_text_turn)
    assert "is_callback_line(cleaned)" in src
    assert "_busy_outro(language)" in src
    busy_pos = src.index("is_callback_line(cleaned)")
    gate_pos = src.index("gate_failed(")
    verbatim_pos = src.index("_deliver_verbatim_question")
    assert busy_pos < gate_pos < verbatim_pos  # busy cut runs before both lanes


# ── bucket-targeted re-run (legacy blob path, end to end through the service) ──


def _campaign(contacts):
    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign

    return OutboundCampaign(
        id=uuid.uuid4(),
        tenant_id="tenant-1",
        name="Busy test",
        status=CampaignStatus.completed,
        contacts=contacts,
        agent_config={"bulk_csv": True, "deterministic": True},
    )


def _contacts_fixture():
    # A qualified, B busy (answered + callback), C answered not-interested,
    # D no_answer, E failed, F pending.
    return [
        {"phone": "919000000001", "name": "A", "status": "answered", "answered_at": "2026-07-15T10:00:00Z",
         "call_link_id": "la", "qualified": True, "lead_score": 4},
        {"phone": "919000000002", "name": "B", "status": "answered", "answered_at": "2026-07-15T10:01:00Z",
         "call_link_id": "lb", "qualified": False, "lead_score": 0, "callback_requested": True},
        {"phone": "919000000003", "name": "C", "status": "answered", "answered_at": "2026-07-15T10:02:00Z",
         "call_link_id": "lc", "qualified": False, "lead_score": 1},
        {"phone": "919000000004", "name": "D", "status": "no_answer", "call_link_id": "ld"},
        {"phone": "919000000005", "name": "E", "status": "failed", "call_link_id": "le"},
        {"phone": "919000000006", "name": "F", "status": "pending", "call_link_id": "lf"},
    ]


class _FakeDB:
    def add(self, *_a, **_k):
        pass

    async def commit(self):
        pass

    async def refresh(self, *_a, **_k):
        pass


@pytest.fixture()
def _rerun_env(monkeypatch):
    from app.services import outbound_campaign_service as svc
    from app.services.plivo_service import PlivoService

    monkeypatch.setattr(PlivoService, "bulk_calling_enabled", staticmethod(lambda tr: True))
    monkeypatch.setattr(PlivoService, "bulk_calling_caller_id", staticmethod(lambda tr: "+919999999999"))
    monkeypatch.setattr(PlivoService, "bulk_calling_auth", staticmethod(lambda tr: ("id", "token")))

    async def _no_other(db, tenant_id, exclude_id=None):
        return None

    async def _no_lock(db, campaign_id):
        return None

    async def _scrub(contacts):
        return contacts, 0

    dialed = {"n": 0}

    async def _dial(campaign, db, **kwargs):
        dialed["n"] += 1

    monkeypatch.setattr(
        svc.OutboundCampaignService, "_assert_no_other_active_campaign", staticmethod(_no_other)
    )
    monkeypatch.setattr(svc.OutboundCampaignService, "_lock_campaign", staticmethod(_no_lock))
    monkeypatch.setattr(svc, "_scrub_dnd", _scrub)
    monkeypatch.setattr(svc.OutboundCampaignService, "_dial_pending", staticmethod(_dial))
    return svc, dialed


def _rerun(svc, campaign, buckets):
    return svc.OutboundCampaignService.rerun_bulk_campaign(
        campaign,
        _FakeDB(),
        tenant_res=object(),
        public_base_url="https://x.example.com",
        path_prefix="/api/nokvo-one/agents",
        buckets=buckets,
    )


def _by_name(campaign):
    return {ct["name"]: ct for ct in campaign.contacts}


def test_rerun_busy_only_rearm(_rerun_env):
    svc, dialed = _rerun_env
    campaign = _campaign(_contacts_fixture())
    out = _run(_rerun(svc, campaign, ["busy"]))
    cts = _by_name(out)
    assert len(cts) == 6
    # B (busy) re-armed with the verdict wiped; fresh link id.
    assert cts["B"]["status"] == "pending"
    assert cts["B"]["call_link_id"] != "lb"
    assert "callback_requested" not in cts["B"] and "lead_score" not in cts["B"]
    # A (qualified) and C (not interested) untouched.
    assert cts["A"]["status"] == "answered" and cts["A"]["qualified"] is True
    assert cts["C"]["status"] == "answered"
    # Misses KEPT as-is on a busy-only re-run (still re-runnable later).
    assert cts["D"]["status"] == "no_answer" and cts["D"]["call_link_id"] == "ld"
    assert cts["E"]["status"] == "failed"
    # Pending always resumes (fresh row).
    assert cts["F"]["status"] == "pending"
    assert out.answered_count == 2  # A + C
    assert out.failed_count == 1    # E kept its failed status
    assert str(out.status) in ("CampaignStatus.running", "running")
    assert dialed["n"] == 1


def test_rerun_no_pickup_only_keeps_busy(_rerun_env):
    svc, _ = _rerun_env
    campaign = _campaign(_contacts_fixture())
    out = _run(_rerun(svc, campaign, ["no_pickup"]))
    cts = _by_name(out)
    # D/E/F re-armed; B (busy) untouched and still flagged.
    assert cts["D"]["status"] == "pending" and cts["D"]["call_link_id"] != "ld"
    assert cts["E"]["status"] == "pending"
    assert cts["F"]["status"] == "pending"
    assert cts["B"]["status"] == "answered" and cts["B"]["callback_requested"] is True
    assert out.answered_count == 3  # A + B + C


def test_rerun_both_buckets(_rerun_env):
    svc, _ = _rerun_env
    campaign = _campaign(_contacts_fixture())
    out = _run(_rerun(svc, campaign, ["no_pickup", "busy"]))
    cts = _by_name(out)
    pending = [n for n, ct in cts.items() if ct["status"] == "pending"]
    assert sorted(pending) == ["B", "D", "E", "F"]
    assert cts["A"]["status"] == "answered" and cts["C"]["status"] == "answered"
    assert out.answered_count == 2


def test_rerun_rejects_unknown_bucket(_rerun_env):
    svc, _ = _rerun_env
    campaign = _campaign(_contacts_fixture())
    with pytest.raises(ValueError):
        _run(_rerun(svc, campaign, ["qualified"]))


def test_rerun_busy_only_with_nothing_to_call_409s(_rerun_env):
    svc, _ = _rerun_env
    # No busy contacts, no pending — only an answered lead and a miss.
    campaign = _campaign([
        {"phone": "919000000001", "name": "A", "status": "answered", "answered_at": "x", "call_link_id": "la"},
        {"phone": "919000000004", "name": "D", "status": "no_answer", "call_link_id": "ld"},
    ])
    with pytest.raises(ValueError):
        _run(_rerun(svc, campaign, ["busy"]))


# ── stamped verdicts land in the right frontend bucket (shape contract) ──


def test_busy_bucket_sql_shapes():
    from app.services.campaign_contacts_v2 import BUCKET_SQL

    assert "callback_requested" in BUCKET_SQL["busy"]
    assert "qualified = false" in BUCKET_SQL["busy"]
    # not_interested must EXCLUDE busy so the buckets stay mutually exclusive.
    assert "callback_requested" in BUCKET_SQL["not_interested"]
    assert "<> 'true'" in BUCKET_SQL["not_interested"]
