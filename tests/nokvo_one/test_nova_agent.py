"""Nova agent loop — protocol, role filtering, tool execution, loop bounds.

LLM + Redis are mocked; these are pure service-level units.
"""
import json

import pytest

from app.services import nova_agent_service as nova
from app.services import nova_session_store as store


# ── helpers ───────────────────────────────────────────────────────────────────

class _FakeUser:
    organization_id = "00000000-0000-0000-0000-000000000001"
    role = "admin"


class _FakeMemberUser(_FakeUser):
    role = "member"


class _FakeTenantRes:
    tenant_id = "t1"
    redis_namespace = "tenant:t1"


def _mock_store(monkeypatch):
    """In-memory replacement for the Redis session store."""
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
    monkeypatch.setattr(
        nova.store.AgentSessionStore, "namespace", staticmethod(lambda tr: tr.redis_namespace)
    )
    return sessions


def _mock_llm(monkeypatch, responses: list[str]):
    calls = {"messages": []}

    async def fake_llm(messages, tenant_res):
        calls["messages"].append(messages)
        return responses.pop(0) if responses else "I'm out of ideas."

    monkeypatch.setattr(nova, "_llm", fake_llm)
    return calls


# ── protocol ─────────────────────────────────────────────────────────────────

def test_parse_tool_call_fence_and_bare():
    fenced = '```json\n{"tool": "lookup_legal", "arguments": {"query": "refunds"}}\n```'
    assert nova.parse_tool_call(fenced) == ("lookup_legal", {"query": "refunds"})
    bare = '{"tool": "get_account_status", "arguments": {}}'
    assert nova.parse_tool_call(bare) == ("get_account_status", {})
    assert nova.parse_tool_call("plain text reply") is None
    assert nova.parse_tool_call('```json\n{"not": "a tool"}\n```') is None


def test_validate_args_required_and_extras():
    tool = next(t for t in nova.TOOLS if t.key == "lookup_legal")
    assert nova.validate_args(tool, {}) == "Missing required fields: query"
    assert nova.validate_args(tool, {"query": "refunds", "bogus": 1}).startswith("Unsupported fields")
    assert nova.validate_args(tool, {"query": "refunds", "doc": "nope"}).startswith("Field doc must be")
    assert nova.validate_args(tool, {"query": "refunds", "doc": "tos"}) is None


def test_tool_schemas_are_valid_json_objects():
    for tool in nova.TOOLS:
        assert tool.input_schema.get("type") == "object"
        json.dumps(tool.input_schema)  # serializable


# ── role filtering ────────────────────────────────────────────────────────────

def test_member_never_sees_admin_tools():
    member_keys = {t.key for t in nova.tools_for_role("member")}
    admin_keys = {t.key for t in nova.tools_for_role("admin")}
    assert member_keys <= admin_keys
    for t in nova.TOOLS:
        if t.admin_only:
            assert t.key not in member_keys


def test_system_prompt_role_split():
    admin_prompt = nova.build_system_prompt("admin", store.empty_state())
    member_prompt = nova.build_system_prompt("member", store.empty_state())
    assert "draft calling campaigns" in admin_prompt
    assert "admin-only" in member_prompt


# ── the loop ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plain_reply_persists_history(monkeypatch):
    sessions = _mock_store(monkeypatch)
    _mock_llm(monkeypatch, ["APEX dials 9 AM to 7 PM IST."])
    res = await nova.nova_turn(None, _FakeTenantRes(), _FakeUser(), None, "When do calls go out?")
    assert "9 AM" in res.reply
    state = sessions[f"tenant:t1:{res.session_id}"]
    assert [h["role"] for h in state["history"]] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_tool_call_executes_and_feeds_result_back(monkeypatch):
    _mock_store(monkeypatch)
    calls = _mock_llm(monkeypatch, [
        '```json\n{"tool": "lookup_legal", "arguments": {"query": "refund policy"}}\n```',
        "All fees are non-refundable per Terms section 6.",
    ])
    res = await nova.nova_turn(None, _FakeTenantRes(), _FakeUser(), None, "can I get a refund?")
    assert res.tool_calls == ["lookup_legal"]
    assert "non-refundable" in res.reply
    # Second LLM call must carry the tool result marked UNTRUSTED.
    second = calls["messages"][1]
    assert any("UNTRUSTED" in m["content"] for m in second if m["role"] == "user")


@pytest.mark.asyncio
async def test_unknown_tool_is_corrected_not_executed(monkeypatch):
    _mock_store(monkeypatch)
    _mock_llm(monkeypatch, [
        '```json\n{"tool": "delete_everything", "arguments": {}}\n```',
        "Sorry, I can't do that.",
    ])
    res = await nova.nova_turn(None, _FakeTenantRes(), _FakeUser(), None, "wipe my account")
    assert res.tool_calls == []
    assert "Sorry" in res.reply


@pytest.mark.asyncio
async def test_loop_bound_stops_runaway_tools(monkeypatch):
    _mock_store(monkeypatch)
    # Model insists on an invalid call forever → loop must exit with the fallback.
    _mock_llm(monkeypatch, ['```json\n{"tool": "lookup_legal", "arguments": {}}\n```'] * 10)
    res = await nova.nova_turn(None, _FakeTenantRes(), _FakeUser(), None, "hmm")
    assert "smaller ask" in res.reply


@pytest.mark.asyncio
async def test_llm_failure_degrades_gracefully(monkeypatch):
    _mock_store(monkeypatch)

    async def boom(messages, tenant_res):
        raise RuntimeError("pool down")

    monkeypatch.setattr(nova, "_llm", boom)
    res = await nova.nova_turn(None, _FakeTenantRes(), _FakeUser(), None, "hello")
    assert "snag" in res.reply
