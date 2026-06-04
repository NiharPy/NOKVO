"""Pytest entry for the deterministic eval / replay framework.

Each JSON fixture in ``tests/nokvo_one/eval/transcripts/`` becomes a
parametrised test case. The runner (see :mod:`tests.nokvo_one.eval.runner`)
walks every user turn through ConversationalMemory → tool_flow_policy →
FSM current_mode → compose_strategy_block, then checks the per-turn
assertions declared in the fixture.

Adding a new regression test is just dropping another JSON file in
``transcripts/`` — no Python required.
"""
from __future__ import annotations

import pytest

from tests.nokvo_one.eval.runner import assert_fixture, list_fixtures


@pytest.mark.parametrize("fixture_name", list_fixtures())
def test_eval_fixture(fixture_name: str) -> None:
    assert_fixture(fixture_name)
