"""Nova tickets + diagnostics — minting, confirm execution, org scoping.

DB, Redis, and email are all mocked; these exercise the service contracts.
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


def _mock_diagnosis(monkeypatch):
    async def fake_diag(db, tenant_res, org_id):
        return {
            "llm_view": {
                "wallet": {"credits_remaining": 120.5},
                "campaigns": [],
                "recent_calls": [{"when": "2026-07-06 10:00", "kind": "outbound", "dur_s": 30}],
                "dial_error_causes": [{"cause": "NO_ANSWER", "count": 7}],
            },
            "ticket_json": {"wallet": {"credits_remaining": 120.5},
                            "recent_calls": [{"trace_id": "abc123", "dur_s": 30}]},
        }

    import app.services.nova_diagnosis_service as diag_mod

    monkeypatch.setattr(diag_mod, "build_tenant_diagnosis", fake_diag)


# ── ticket minting via the loop ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ticket_tool_mints_card_without_persisting(monkeypatch):
    sessions = _mock_store(monkeypatch)
    _mock_diagnosis(monkeypatch)

    async def fake_llm(messages, tenant_res):
        return ('```json\n{"tool": "create_support_ticket", "arguments": '
                '{"subject": "Calls failing since morning", "description": "Every dial shows failed after one ring."}}\n```')

    monkeypatch.setattr(nova, "_llm", fake_llm)
    res = await nova.nova_turn(None, _FakeTenantRes(), _FakeUser(), None, "my calls keep failing, raise a ticket")

    assert res.tool_calls == ["create_support_ticket"]
    assert len(res.cards) == 1
    card = res.cards[0]
    assert card["type"] == "ticket_preview"
    assert card["subject"] == "Calls failing since morning"
    assert "NO_ANSWER" in card["diagnosis_summary"]
    # Parked, not persisted: pending_action holds the payload incl. FULL diagnosis.
    state = sessions[f"tenant:t1:{res.session_id}"]
    pending = state["pending_action"]
    assert pending["action_id"] == card["action_id"]
    assert pending["type"] == "create_ticket"
    assert pending["payload"]["diagnosis"]["recent_calls"][0]["trace_id"] == "abc123"


@pytest.mark.asyncio
async def test_llm_view_never_contains_trace_ids(monkeypatch):
    _mock_store(monkeypatch)
    _mock_diagnosis(monkeypatch)
    seen = {}

    async def fake_llm(messages, tenant_res):
        seen["all"] = json.dumps(messages)
        if "tool_result" in seen["all"]:
            return "Your last calls mostly rang out (NO_ANSWER x7)."
        return '```json\n{"tool": "get_diagnostics", "arguments": {}}\n```'

    monkeypatch.setattr(nova, "_llm", fake_llm)
    res = await nova.nova_turn(None, _FakeTenantRes(), _FakeUser(), None, "why are my calls failing?")
    assert res.tool_calls == ["get_diagnostics"]
    assert "abc123" not in seen["all"]  # trace_id stays out of the prompt


# ── confirm execution ─────────────────────────────────────────────────────────

class _TicketDB:
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def execute(self, stmt):
        class _R:
            def scalar_one_or_none(self):
                return "Acme Estates"
        return _R()


@pytest.mark.asyncio
async def test_execute_create_ticket_persists_and_emails(monkeypatch):
    sent = {}

    async def fake_email(**kw):
        sent.update(kw)

    from app.services.email_service import EmailService

    monkeypatch.setattr(EmailService, "send_apex_support_ticket_email", staticmethod(fake_email))

    db = _TicketDB()
    payload = {"subject": "Stuck campaign", "description": "It says running but nothing dials.",
               "diagnosis": {"recent_calls": []}}
    result, reply = await nova._execute_create_ticket(db, _FakeTenantRes(), _FakeUser(), payload)

    assert db.committed and len(db.added) == 1
    ticket = db.added[0]
    assert ticket.subject == "Stuck campaign"
    assert ticket.diagnosis == {"recent_calls": []}
    assert ticket.requested_by_email == "admin@example.com"
    assert result["status"] == "open"
    assert result["ticket_id"] in reply or result["ticket_id"][:8] in reply
    assert sent["org_name"] == "Acme Estates"


@pytest.mark.asyncio
async def test_email_failure_does_not_lose_ticket(monkeypatch):
    async def boom(**kw):
        raise RuntimeError("smtp down")

    from app.services.email_service import EmailService

    monkeypatch.setattr(EmailService, "send_apex_support_ticket_email", staticmethod(boom))
    db = _TicketDB()
    result, _ = await nova._execute_create_ticket(
        db, _FakeTenantRes(), _FakeUser(), {"subject": "x" * 10, "description": "y" * 20, "diagnosis": {}}
    )
    assert db.committed and result["status"] == "open"


# ── diagnosis compaction ──────────────────────────────────────────────────────

def test_redact_phone():
    from app.services.nova_diagnosis_service import _redact_phone

    assert _redact_phone("919876543210") == "…3210"
    assert _redact_phone("91") == "…"
    assert _redact_phone(None) == "…"


@pytest.mark.asyncio
async def test_diagnosis_block_failure_is_isolated(monkeypatch):
    """One failing block reports unavailable; the rest still assemble."""
    import app.services.nova_diagnosis_service as diag

    async def ok_wallet(db, org_id):
        return {"credits_remaining": 10.0, "estimated_minutes_remaining": 2}

    async def boom(*a, **k):
        raise RuntimeError("db off")

    monkeypatch.setattr(diag, "_wallet", ok_wallet)
    monkeypatch.setattr(diag, "_campaigns", boom)
    monkeypatch.setattr(diag, "_recent_calls", boom)
    monkeypatch.setattr(diag, "_dial_errors", boom)

    out = await diag.build_tenant_diagnosis(None, _FakeTenantRes(), _FakeUser().organization_id)
    assert out["llm_view"]["wallet"]["credits_remaining"] == 10.0
    assert out["ticket_json"]["campaigns"] == {"unavailable": True}
