"""Campaign content guard — the "company A degrades company B" defense.

Covers the moderation service end to end at the unit level (DB-less, LLM
mocked): the text-surface collector, the tolerant verdict parser, fail-open on
every LLM failure mode, and the off/warn/enforce gate semantics including the
operator alert email. The endpoint wiring reuses the pre-existing
ValueError→400 channel (`_safe_detail`), which has its own coverage.
"""
import asyncio
import json

import pytest

from app.services import campaign_moderation_service as cms
from app.services.campaign_moderation_service import (
    ModerationVerdict,
    _parse_verdict,
    collect_campaign_text,
    moderate_campaign_content,
    require_campaign_content_allowed,
)


class _Org:
    """Duck-typed Organization: only the fields the guard reads."""

    id = "org-1"
    name = "My Home Constructions"
    legal_name = "My Home Constructions Pvt Ltd"
    alias_name = "My Home"
    product_tier = "nokvo_apex"


def _full_agent_config() -> dict:
    return {
        "agent_prompt": "MARKER_PROMPT premium 3BHK flats in Kokapet",
        "company_name": "MARKER_COMPANY",
        "caller_name": "MARKER_CALLER",
        "objectives": ["MARKER_OBJECTIVE"],
        "exit_conditions": ["MARKER_EXIT"],
        "questionnaire": {
            "intro": "MARKER_INTRO",
            "outro": "MARKER_OUTRO",
            "intro_source": "MARKER_INTRO_SOURCE",
            "intro_i18n": {"te": "MARKER_INTRO_TE"},
            "outro_i18n": {"hi": "MARKER_OUTRO_HI"},
            "questions": [
                {
                    "id": "q1",
                    "type": "answer",
                    "text": "MARKER_QTEXT",
                    "text_source": "MARKER_QSOURCE",
                    "desired_answer": "MARKER_DESIRED",
                    "text_i18n": {"hi": "MARKER_QTEXT_HI"},
                    "tiers": [{"id": "t1", "label": "MARKER_TIER"}],
                },
            ],
        },
    }


def _chat_recorder(monkeypatch, reply: str):
    """Patch the pool client; return the dict the fake fills with call args."""
    from app.services.llm_pool import LLMPoolClient

    seen = {}

    async def fake_chat(messages, *, max_tokens=120, temperature=0.3, est_tokens=None):
        seen["messages"] = messages
        seen["calls"] = seen.get("calls", 0) + 1
        return reply

    monkeypatch.setattr(LLMPoolClient, "chat", fake_chat)
    return seen


def _alert_recorder(monkeypatch):
    from app.services.email_service import EmailService

    sent = {}

    async def fake_alert(**kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(EmailService, "send_campaign_moderation_alert", fake_alert)
    return sent


async def _drain_bg_tasks():
    """The alert email is fire-and-forget; let it finish before asserting."""
    for _ in range(3):
        pending = list(cms._bg_tasks)
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


# ── Text-surface collector ───────────────────────────────────────────────────
def test_collect_campaign_text_covers_every_tenant_field():
    pairs = collect_campaign_text("MARKER_NAME", _full_agent_config())
    blob = "\n".join(text for _label, text in pairs)
    for marker in (
        "MARKER_NAME", "MARKER_PROMPT", "MARKER_COMPANY", "MARKER_CALLER",
        "MARKER_OBJECTIVE", "MARKER_EXIT", "MARKER_INTRO", "MARKER_OUTRO",
        "MARKER_INTRO_SOURCE", "MARKER_INTRO_TE", "MARKER_OUTRO_HI",
        "MARKER_QTEXT", "MARKER_QSOURCE", "MARKER_DESIRED", "MARKER_QTEXT_HI",
        "MARKER_TIER",
    ):
        assert marker in blob, f"{marker} missing from the moderated text surface"


def test_collect_campaign_text_drops_blanks_and_handles_missing_config():
    assert collect_campaign_text("Camp", None) == [("campaign_name", "Camp")]
    assert collect_campaign_text("", {}) == []


# ── Verdict parser ───────────────────────────────────────────────────────────
def test_parse_verdict_allowed_and_flagged():
    assert _parse_verdict('{"allowed": true, "category": "none", "reason": ""}').allowed is True
    v = _parse_verdict(
        '{"allowed": false, "category": "third_party_disparagement", "reason": "attacks Aparna"}'
    )
    assert v.allowed is False and v.category == "third_party_disparagement"
    assert "Aparna" in v.reason


def test_parse_verdict_tolerates_markdown_fences():
    raw = '```json\n{"allowed": false, "category": "impersonation", "reason": "claims to be Aparna"}\n```'
    v = _parse_verdict(raw)
    assert v.allowed is False and v.category == "impersonation"


def test_parse_verdict_rejects_garbage_and_normalizes_bad_category():
    assert _parse_verdict("not json at all") is None
    assert _parse_verdict('{"category": "x"}') is None  # no boolean "allowed"
    v = _parse_verdict('{"allowed": false, "category": "made_up", "reason": "r"}')
    assert v.allowed is False and v.category == "third_party_disparagement"


# ── Verdict call: wiring + fail-open ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_moderate_sends_trusted_identity_and_untrusted_campaign_text(monkeypatch):
    seen = _chat_recorder(monkeypatch, '{"allowed": true, "category": "none", "reason": ""}')
    verdict = await moderate_campaign_content(
        _Org(), campaign_name="Kokapet launch", agent_config=_full_agent_config()
    )
    assert verdict.allowed is True and verdict.skipped is False
    system = seen["messages"][0]["content"]
    user = seen["messages"][1]["content"]
    # The registered (KYC) identity is the trusted anchor for the impersonation check…
    assert "My Home Constructions Pvt Ltd" in user and "Registered tenant" in user
    # …and the tenant-authored text is explicitly untrusted data, never instructions.
    assert "UNTRUSTED" in system and "UNTRUSTED" in user
    assert "impersonation" in system and "third_party_disparagement" in system
    assert "MARKER_QTEXT_HI" in user  # hand-editable i18n reaches the reviewer


@pytest.mark.asyncio
async def test_moderate_flags_disparagement(monkeypatch):
    _chat_recorder(
        monkeypatch,
        json.dumps(
            {
                "allowed": False,
                "category": "third_party_disparagement",
                "reason": "the script attacks Aparna's projects",
            }
        ),
    )
    verdict = await moderate_campaign_content(
        _Org(),
        campaign_name="Truth about Aparna",
        agent_config={"agent_prompt": "Aparna towers are all delayed, buy My Home instead"},
    )
    assert verdict.allowed is False
    assert verdict.category == "third_party_disparagement"


@pytest.mark.asyncio
async def test_moderate_fails_open_on_llm_error(monkeypatch):
    from app.services.llm_pool import LLMPoolClient

    async def boom(messages, **_kw):
        raise RuntimeError("pool down")

    monkeypatch.setattr(LLMPoolClient, "chat", boom)
    verdict = await moderate_campaign_content(
        _Org(), campaign_name="Camp", agent_config={"agent_prompt": "hello"}
    )
    assert verdict.allowed is True and verdict.skipped is True


@pytest.mark.asyncio
async def test_moderate_fails_open_on_timeout(monkeypatch):
    from app.services.llm_pool import LLMPoolClient

    async def slow(messages, **_kw):
        await asyncio.sleep(5)
        return "{}"

    monkeypatch.setattr(LLMPoolClient, "chat", slow)
    verdict = await moderate_campaign_content(
        _Org(), campaign_name="Camp", agent_config={"agent_prompt": "hello"}, timeout_ms=50
    )
    assert verdict.allowed is True and verdict.skipped is True


@pytest.mark.asyncio
async def test_moderate_fails_open_on_unparseable_reply(monkeypatch):
    _chat_recorder(monkeypatch, "I think this campaign is probably fine?")
    verdict = await moderate_campaign_content(
        _Org(), campaign_name="Camp", agent_config={"agent_prompt": "hello"}
    )
    assert verdict.allowed is True and verdict.skipped is True


# ── Gate semantics: off / warn / enforce ─────────────────────────────────────
@pytest.mark.asyncio
async def test_gate_off_mode_never_calls_llm(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "CAMPAIGN_CONTENT_MODERATION", "off")
    seen = _chat_recorder(monkeypatch, "{}")
    verdict = await require_campaign_content_allowed(
        _Org(), campaign_name="Camp", agent_config={"agent_prompt": "anything"}
    )
    assert verdict.allowed is True and verdict.skipped is True
    assert seen.get("calls") is None


@pytest.mark.asyncio
async def test_gate_enforce_blocks_flagged_campaign_and_alerts(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "CAMPAIGN_CONTENT_MODERATION", "enforce")
    # A hard-block category (disparagement) walls the campaign in enforce mode.
    _chat_recorder(
        monkeypatch,
        '{"allowed": false, "category": "third_party_disparagement", '
        '"reason": "attacks Aparna\'s projects"}',
    )
    sent = _alert_recorder(monkeypatch)
    with pytest.raises(ValueError) as exc:
        await require_campaign_content_allowed(
            _Org(),
            campaign_name="Aparna update",
            agent_config={"agent_prompt": "Aparna towers are all delayed, buy from us"},
            actor_email="admin@myhome.example",
        )
    # Tenant-showable, routes through the endpoints' ValueError→400 channel.
    assert "attacks Aparna's projects" in str(exc.value)
    assert "disparage" in str(exc.value)
    await _drain_bg_tasks()
    assert sent["category"] == "third_party_disparagement"
    assert sent["actor_email"] == "admin@myhome.example"
    assert sent["mode"] == "enforce"
    assert "Aparna" in sent["content_snippet"]


@pytest.mark.asyncio
async def test_gate_enforce_impersonation_allows_and_alerts(monkeypatch):
    """A possible-impersonation flag no longer walls the tenant (dealership /
    brand-reseller / JV-partner false positives live here): it's allowed through
    with an ops alert for spot-review. Disparagement/scams still hard-block."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "CAMPAIGN_CONTENT_MODERATION", "enforce")
    _chat_recorder(
        monkeypatch,
        '{"allowed": false, "category": "impersonation", "reason": "names Toyota"}',
    )
    sent = _alert_recorder(monkeypatch)
    # No raise — the campaign saves.
    verdict = await require_campaign_content_allowed(
        _Org(),
        campaign_name="Toyota festive offers",
        agent_config={"company_name": "Toyota"},
        actor_email="admin@sunrisemotors.example",
    )
    assert verdict.allowed is False  # flag reported, not enforced
    assert verdict.category == "impersonation"
    await _drain_bg_tasks()
    assert sent["category"] == "impersonation"  # ops still gets the spot-review alert
    assert sent["mode"] == "enforce"


@pytest.mark.asyncio
async def test_gate_warn_mode_allows_through_but_alerts(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "CAMPAIGN_CONTENT_MODERATION", "warn")
    _chat_recorder(
        monkeypatch,
        '{"allowed": false, "category": "third_party_disparagement", "reason": "attacks a rival"}',
    )
    sent = _alert_recorder(monkeypatch)
    verdict = await require_campaign_content_allowed(
        _Org(), campaign_name="Camp", agent_config={"agent_prompt": "rival is a fraud"}
    )
    assert verdict.allowed is False  # verdict reported, not raised
    await _drain_bg_tasks()
    assert sent["mode"] == "warn"


@pytest.mark.asyncio
async def test_gate_enforce_allows_clean_campaign_without_alert(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "CAMPAIGN_CONTENT_MODERATION", "enforce")
    _chat_recorder(monkeypatch, '{"allowed": true, "category": "none", "reason": ""}')
    sent = _alert_recorder(monkeypatch)
    verdict = await require_campaign_content_allowed(
        _Org(), campaign_name="Kokapet launch", agent_config=_full_agent_config()
    )
    assert verdict.allowed is True
    await _drain_bg_tasks()
    assert sent == {}


# ── Config + prompt hardening regressions ────────────────────────────────────
def test_security_validator_flags_moderation_not_enforced(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(config.settings, "CAMPAIGN_CONTENT_MODERATION", "off")
    warns = " ".join(config.validate_security_config()).lower()
    assert "campaign_content_moderation" in warns


def test_outbound_prompt_carries_anti_disparagement_rule():
    from app.services.agent_outbound_context import _OUTBOUND_UNIVERSAL_TURN_RULES

    rules = _OUTBOUND_UNIVERSAL_TURN_RULES
    assert "NEVER ATTACK OR IMPERSONATE" in rules
    assert "Never disparage" in rules
