"""Regression: a short reply that ANSWERS the agent's question (e.g. "10 AM"
after "what time?", or "Yeah, sure" after "send it to WhatsApp?") must reach the
LLM — it must NOT get the "Mm-hm, go on." filler nudge.

Real-estate inbound now runs WITHOUT a tool_flow (site visits are
conversational), so the old "in active booking flow" guard no longer covered
these answers and the agent ate them with the filler nudge, making the caller
repeat themselves (seen in the LangSmith traces).
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.services.agent_runtime_bundle import RuntimeBundle
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _bundle() -> RuntimeBundle:
    return RuntimeBundle(
        tenant_id="tenant-test",
        organization=SimpleNamespace(industry="real_estate", name="Raghava Estates"),
        organization_industry="real_estate",
        organization_name="Raghava Estates",
        overrides={},
        custom_tabs=[],
        policy_cards=[],
        single_prompt_guidance="You are a concise real estate voice agent for Raghava Skyline.",
        single_prompt_enabled=True,
        version_key="test",
    )


def _tenant():
    return SimpleNamespace(
        tenant_id="tenant-test",
        organization_id=uuid.uuid4(),
        provider_status={
            "single_prompt_voice_agent": {
                "enabled": True,
                "prompt": "You are a concise real estate voice agent for Raghava Skyline.",
            }
        },
    )


def _route(user_text: str, last_assistant: str) -> dict:
    return _run(
        NokvoOneVoicePipeline._route_turn(
            _tenant(),
            user_text,
            language="en",
            company_name="Raghava Estates",
            call_id=f"call-{uuid.uuid4()}",
            db=SimpleNamespace(),
            turn_cache={
                "history": [{"role": "assistant", "content": last_assistant}],
                "state": {},
                "bundle": _bundle(),
            },
        )
    )


def test_time_answer_after_question_is_not_filler_nudge():
    route = _route("10 AM", "Which day and time works best for your site visit?")
    assert route.get("answer") != "Mm-hm, go on."
    assert route["route"] in ("rag", "smalltalk_llm")


def test_yes_answer_after_whatsapp_offer_is_not_filler_nudge():
    route = _route(
        "Yeah, sure",
        "Would you like me to send the brochure and location to your WhatsApp after we hang up?",
    )
    assert route.get("answer") != "Mm-hm, go on."


def test_same_short_input_still_nudges_when_no_question_was_asked():
    """Control: the SAME '10 AM' input that we now let through after a question
    still gets the gentle nudge when the agent did NOT just ask anything — i.e.
    the guard is scoped to answers-to-a-question, not blanket-disabled."""
    route = _route("10 AM", "Skyline Heights has 3 and 4 BHK apartments in Tukkuguda.")
    assert route["route"] == "template"
    assert route.get("answer") == "Mm-hm, go on."
