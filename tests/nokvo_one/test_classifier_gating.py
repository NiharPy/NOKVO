"""Tier-2 LLM intent classifier is OFF the first-audio path for KB-only verticals.

The blocking classify (up to ~2.5s) only earns its latency on tenants with
policy cards (it routes cancellation/refund intents the regex missed to the
policy engine). Real-estate / clinics have no policy cards, so the turn must
skip the classify and route straight to RAG — saving the await.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.services.agent_runtime_bundle import RuntimeBundle
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.llm_intent_classifier import ClassifiedIntent, INTENT_KB_QUESTION


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _bundle() -> RuntimeBundle:
    return RuntimeBundle(
        tenant_id="t",
        organization=SimpleNamespace(industry="real_estate", name="Raghava"),
        organization_industry="real_estate",
        organization_name="Raghava",
        overrides={},
        custom_tabs=[],
        policy_cards=[],
        single_prompt_guidance="",
        single_prompt_enabled=False,
        version_key="t",
    )


def _route(monkeypatch, *, policy_cards):
    calls: list[int] = []

    async def fake_retrieve(*a, **k):
        return {"chunks": [], "citations": []}

    async def fake_classify(*a, **k):
        calls.append(1)
        return ClassifiedIntent(
            intent=INTENT_KB_QUESTION, needs_kb=True, sensitive=False, sentiment="neutral"
        )

    monkeypatch.setattr(NokvoOneVoicePipeline, "retrieve", staticmethod(fake_retrieve))
    monkeypatch.setattr(
        NokvoOneVoicePipeline, "_active_policy_cards", staticmethod(lambda tr: policy_cards)
    )
    monkeypatch.setattr(
        "app.services.llm_intent_classifier.LLMIntentClassifier.classify", fake_classify
    )

    tenant_res = SimpleNamespace(tenant_id="t", organization_id=uuid.uuid4(), provider_status={})
    route = _run(
        NokvoOneVoicePipeline._route_turn(
            tenant_res,
            "what is the possession date for the project",
            language="en",
            company_name="Raghava",
            call_id=f"c-{uuid.uuid4()}",
            db=SimpleNamespace(),
            turn_cache={"history": [], "state": {}, "bundle": _bundle()},
        )
    )
    return route, calls


def test_no_policy_cards_skips_blocking_classify(monkeypatch):
    route, calls = _route(monkeypatch, policy_cards=[])
    assert calls == []                 # the ~2.5s classify was NOT awaited
    assert route["route"] == "rag"     # routed straight to grounded retrieval


def test_policy_card_tenant_still_classifies(monkeypatch):
    route, calls = _route(monkeypatch, policy_cards=[{"id": "c1"}])
    assert calls == [1]                # policy verticals keep the classifier
