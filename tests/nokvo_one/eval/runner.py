"""Deterministic replay runner for historical voice transcripts.

The pipeline's behaviour is a stack of layers:

  1. ``ConversationalMemory.merge_text`` — extracts facts / objections /
     commitments / preferences / salient notes from a caller turn.
  2. ``tool_flow_policy.evaluate_tool_flow_policy`` — decides which
     slot-fill flow (real_estate_site_visit / leads_create) is active,
     extracts the next slot, returns the canned slot question.
  3. ``real_estate_agent_fsm.current_mode`` (inbound) /
     ``real_estate_outbound_agent_fsm.current_mode`` (outbound) — maps
     the combined state to an explicit mode label.
  4. ``conversation_strategy.compose_strategy_block`` — turns the
     memory into a tactical directive block for the LLM.

The LLM call sits on top of all four. The bugs we keep finding live
in those four deterministic layers, so a replay framework that
exercises THEM (without the LLM) catches the vast majority of
regressions per change.

This runner accepts a transcript fixture (see :mod:`transcripts.*`) and
walks each turn through layers 1–4, building up real ``ConversationalMemory``
and a real ``tool_flow`` state dict as it goes. Per-turn assertions
declared in the fixture are checked at the end of each user turn.

Tests live in :mod:`test_eval_replay`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.conversational_memory import ConversationalMemory
from app.services.conversation_strategy import compose_strategy_block
from app.services.real_estate_agent_fsm import current_mode as inbound_current_mode
from app.services.real_estate_outbound_agent_fsm import (
    current_mode as outbound_current_mode,
)
from app.services.tool_flow_policy import (
    _extract_person_name,
    evaluate_tool_flow_policy,
)


FIXTURE_DIR = Path(__file__).parent / "transcripts"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture by stem (no ``.json`` extension required)."""
    path = FIXTURE_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"No fixture at {path}")
    return json.loads(path.read_text())


def list_fixtures() -> list[str]:
    """Return every fixture name (sorted) for parametrised pytest runs."""
    return sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))


class ReplayResult:
    """Per-turn snapshot the runner emits — usable in assertions."""

    def __init__(self):
        self.memory = ConversationalMemory()
        self.tool_flow_state: dict[str, Any] = {}
        self.turn_history: list[dict[str, str]] = []
        self.per_turn: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        return {
            "memory": self.memory.to_state_blob(),
            "tool_flow": dict(self.tool_flow_state),
            "history_len": len(self.turn_history),
        }


def replay(fixture: dict[str, Any]) -> ReplayResult:
    """Walk every turn in ``fixture`` through the deterministic layers.

    Returns a :class:`ReplayResult` whose ``per_turn`` list carries one
    entry per user turn: the merged memory snapshot, the current
    tool_flow state, the FSM mode, the strategy block, and any name
    extraction the fixture asked us to run.
    """
    business_type = fixture.get("business_type") or "real_estate"
    is_outbound = bool(fixture.get("outbound"))
    objectives = list(fixture.get("objectives") or [])
    result = ReplayResult()

    turn_index = 0
    for raw_turn in fixture.get("turns") or []:
        role = str(raw_turn.get("role") or "user").lower()
        text = str(raw_turn.get("text") or "")
        if not text:
            continue
        if role == "user":
            turn_index += 1
            result.memory.merge_text(
                text,
                turn_index=turn_index,
                role="user",
                business_type=business_type,
            )
            # Evaluate the tool_flow policy with the running state.
            tool_flow = evaluate_tool_flow_policy(
                text,
                business_type=business_type,
                history=result.turn_history,
                state={
                    "tool_flow": dict(result.tool_flow_state),
                    "memory": result.memory.to_state_blob(),
                },
                language="en",
            )
            # Persist the FSM's state_patch into our running tool_flow blob
            # exactly the way the pipeline would via _apply_route_state.
            if isinstance(tool_flow, dict):
                patch = tool_flow.get("state_patch") or {}
                if isinstance(patch.get("tool_flow"), dict):
                    result.tool_flow_state = dict(patch["tool_flow"])
            # Mode is derived from the new state + memory.
            if is_outbound:
                mode = outbound_current_mode(
                    {"tool_flow": result.tool_flow_state},
                    objectives,
                    memory=result.memory,
                )
            else:
                mode = inbound_current_mode(
                    {"tool_flow": result.tool_flow_state},
                    memory=result.memory,
                )
            # Strategy block fires the same way the LLM would see it.
            strategy = compose_strategy_block(
                result.memory,
                business_type=business_type,
                is_outbound=is_outbound,
                company_name=fixture.get("company_name"),
                focus_project=fixture.get("focus_project"),
            )
            # Run the name extractor independently so fixtures can assert
            # on it without needing a slot in tool_flow.
            extracted_name = _extract_person_name(text)
            result.per_turn.append(
                {
                    "turn_index": turn_index,
                    "user_text": text,
                    "mode": mode,
                    "tool_flow_active": bool(result.tool_flow_state.get("active")),
                    "tool_flow_flow_key": result.tool_flow_state.get("flow_key"),
                    "tool_flow_state_slot": (tool_flow or {}).get("state_slot")
                    if isinstance(tool_flow, dict)
                    else None,
                    "tool_flow_answer": (tool_flow or {}).get("answer")
                    if isinstance(tool_flow, dict)
                    else None,
                    "extracted_name": extracted_name,
                    "strategy_block": strategy,
                    "objections_live_codes": [
                        o.get("code")
                        for o in result.memory.objections
                        if not o.get("from_prior_call")
                        and o.get("resolved_at_turn") is None
                    ],
                }
            )
            result.turn_history.append({"role": "user", "content": text})
        else:
            result.turn_history.append({"role": "assistant", "content": text})

    return result


def check_assertion(per_turn: dict[str, Any], assertion: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate one assertion against a per-turn snapshot. Returns
    ``(ok, message)`` so the pytest entry can produce a useful diff."""
    op = str(assertion.get("op") or "equals").lower()
    field = str(assertion.get("field") or "")
    expected = assertion.get("expected")
    actual = per_turn.get(field)
    if op == "equals":
        ok = actual == expected
        return ok, f"{field}: expected {expected!r}, got {actual!r}"
    if op == "contains":
        ok = expected in (actual or "") if isinstance(actual, str) else False
        return ok, f"{field} should contain {expected!r}; got {actual!r}"
    if op == "not_contains":
        ok = expected not in (actual or "") if isinstance(actual, str) else True
        return ok, f"{field} should NOT contain {expected!r}; got {actual!r}"
    if op == "in":
        ok = actual in (expected or [])
        return ok, f"{field}: expected one of {expected!r}, got {actual!r}"
    if op == "is_none":
        ok = actual is None
        return ok, f"{field}: expected None, got {actual!r}"
    if op == "is_not_none":
        ok = actual is not None
        return ok, f"{field}: expected non-None, got None"
    if op == "list_contains":
        ok = expected in (actual or [])
        return ok, f"{field}: list should contain {expected!r}; got {actual!r}"
    if op == "list_not_contains":
        ok = expected not in (actual or [])
        return ok, f"{field}: list should NOT contain {expected!r}; got {actual!r}"
    return False, f"unknown assertion op: {op}"


def assert_fixture(name: str) -> None:
    """Replay a single named fixture and assert every per-turn assertion
    it declares. Raises :class:`AssertionError` with a useful diff when
    anything fails.

    Expectations are indexed by ``turn_index`` (the 1-based caller turn
    number) — fixtures may include expectations for any subset of turns,
    in any order.
    """
    fixture = load_fixture(name)
    result = replay(fixture)
    by_turn_index = {per_turn["turn_index"]: per_turn for per_turn in result.per_turn}
    failures: list[str] = []
    for expected_turn in fixture.get("expected") or []:
        target_index = expected_turn.get("turn_index")
        if not isinstance(target_index, int):
            failures.append(f"expected entry missing turn_index: {expected_turn!r}")
            continue
        per_turn = by_turn_index.get(target_index)
        if per_turn is None:
            failures.append(
                f"turn {target_index}: fixture has expectations for a turn that didn't run "
                f"(ran turns: {sorted(by_turn_index)})"
            )
            continue
        for assertion in expected_turn.get("asserts") or []:
            ok, message = check_assertion(per_turn, assertion)
            if not ok:
                failures.append(
                    f"turn {per_turn['turn_index']} ({per_turn['user_text'][:60]!r}): {message}"
                )
    if failures:
        joined = "\n  - ".join(failures)
        raise AssertionError(f"Fixture '{name}' has {len(failures)} failure(s):\n  - {joined}")
