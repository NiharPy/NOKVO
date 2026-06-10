"""Regression tests for the inbound booking workflow holes (H1/H2/H3).

H1 (confirm-token desync) is resolved transitively by H2 — a confirmation "yes"
that falls through to the next slot is rejected by the extractor, so the slot
stays pending and its prompt fires instead of storing "yes".
"""
from __future__ import annotations

from app.services.tool_flow_policy import _extract_value, evaluate_tool_flow_policy


# ── H2: affirmations / fillers are never data-slot values ───────────────────


def test_affirmations_rejected_for_text_slots():
    for kind in ("project", "reason", "name"):
        for v in ("yes", "yeah sure", "ok", "no", "nope", "correct", "that's right", "go ahead"):
            assert _extract_value(v, kind, kind) is None, f"{kind}/{v!r} should reject"
    # Real values still pass.
    assert _extract_value("Skyline Heights", "project", "project") == "Skyline Heights"
    assert _extract_value("Nihar Reddy", "name", "name") == "Nihar Reddy"


# ── H3: questions are never names (with or without trailing "?") ────────────


def test_questions_not_stored_as_name():
    for q in ("what's the price of a 2BHK?", "what's your timing", "how much is it",
              "where are you located", "what is the location?"):
        assert _extract_value(q, "name", "name") is None, f"{q!r} should reject"
    # A hesitant real name still parses.
    assert _extract_value("uh I think its Priya", "name", "name") == "Priya"


# ── H1: the booking completes cleanly with correct args ─────────────────────


def test_site_visit_booking_completes_with_clean_args():
    state: dict = {}
    history: list = []
    turns = ["I want to book a site visit", "Nihar Reddy", "yes", "9876543210", "yes",
             "Skyline Heights", "tomorrow", "11 am"]
    last = None
    for u in turns:
        res = evaluate_tool_flow_policy(
            u, business_type="real_estate", schema_overrides=None, custom_tabs=None,
            provider_status={}, history=history, state=state, language="en",
        )
        if res is not None:
            for k, v in (res.get("state_patch") or {}).items():
                state[k] = v
            last = res
        history += [{"role": "user", "content": u}, {"role": "assistant", "content": (res or {}).get("answer") or "..."}]

    tf = state.get("tool_flow", {})
    assert tf.get("completed") is True
    collected = tf.get("collected") or {}
    # The confirmation "yes" must NOT have leaked into the project slot.
    assert collected.get("project_name") == "Skyline Heights"
    assert collected.get("name") == "Nihar Reddy"
    assert collected.get("phone") == "9876543210"
    # The completion action carries the booking tool + the same clean args.
    action = last.get("action") or {}
    assert action.get("tool_key") == "qualify_lead_and_schedule_visit"
    assert action.get("arguments", {}).get("project_name") == "Skyline Heights"
