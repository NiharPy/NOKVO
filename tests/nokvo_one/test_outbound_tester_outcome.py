"""Outbound-tester end-of-call routing (_classify_and_persist_tester_outcome).

Regression for two reported bugs:
  * A call that booked a SITE VISIT also created a LEAD — a booking must be
    site-visit-ONLY.
  * Neither record carried the post-call note — the outbound-tester path is
    skipped by both shared condensers, so the note must be written here.

These mock the session/classifier/condenser deps (unit tests run without a DB
or live LLM, per tests/nokvo_one/conftest.py).
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.api import nokvo_one_voice as mod
from app.services.agent_session_store import AgentSessionStore
from app.services.outbound_call_outcome_classifier import (
    OUTCOME_INTERESTED,
    OUTCOME_NOT_INTERESTED,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _WS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class _DB:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def rollback(self):
        pass


class _Memory:
    def __init__(self, name=None, phone=None):
        self._d = {"name": name, "phone": phone}

    def get(self, k):
        return self._d.get(k)

    def has(self, k):
        return self._d.get(k) is not None


def _outcome(o, reason="reason", uncat=False):
    return SimpleNamespace(outcome=o, reason=reason, is_uncategorized=uncat)


def _patch(monkeypatch, *, state, outcome):
    async def get_history(_tr, _cid):
        return [
            {"role": "user", "content": "Saturday 11 AM works"},
            {"role": "assistant", "content": "Great, I'll lock that in."},
        ]

    async def get_state(_tr, _cid):
        return state

    async def classify(_tr, **kw):
        return outcome

    async def load_memory(_tr, _cid):
        return _Memory(name="Nihar", phone="+910000000000")

    monkeypatch.setattr(AgentSessionStore, "get_history", staticmethod(get_history))
    monkeypatch.setattr(AgentSessionStore, "get_state", staticmethod(get_state))
    monkeypatch.setattr(
        "app.services.outbound_call_outcome_classifier.classify_outbound_outcome",
        classify,
    )
    monkeypatch.setattr("app.services.conversational_memory.load_memory", load_memory)

    note_calls = []

    async def fake_note(**kw):
        note_calls.append(kw)

    monkeypatch.setattr(mod, "_write_call_note_to_record", fake_note)
    return note_calls


def _invoke(ws, db, **over):
    kwargs = dict(
        db=db,
        tenant_res=SimpleNamespace(tenant_id="t1", organization_id=uuid.uuid4()),
        call_id="call-1",
        user_id=uuid.uuid4(),
        campaign=None,
        outbound_context=None,
        organization_id=uuid.uuid4(),
    )
    kwargs.update(over)
    return _run(mod._classify_and_persist_tester_outcome(ws, **kwargs))


def test_booked_site_visit_is_site_visit_only(monkeypatch):
    """A booked site visit (auto_site_visit_id in state) must NOT also create a
    lead, and the note must land on the site-visit record."""
    sv_id = str(uuid.uuid4())
    state = {
        "auto_site_visit_created": True,
        "auto_site_visit_id": sv_id,
        "auto_lead_created": True,
    }
    note_calls = _patch(monkeypatch, state=state, outcome=_outcome(OUTCOME_INTERESTED))
    ws, db = _WS(), _DB()
    _invoke(ws, db)

    assert db.added == []  # NO duplicate lead row
    assert ws.sent and ws.sent[0]["site_visit_id"] == sv_id
    assert ws.sent[0]["lead_id"] is None
    assert len(note_calls) == 1 and str(note_calls[0]["record_id"]) == sv_id


def test_interest_without_teardown_record_creates_one_lead_and_notes_it(monkeypatch):
    """Basic interest with nothing filed in teardown → exactly one lead, noted."""
    note_calls = _patch(monkeypatch, state={}, outcome=_outcome(OUTCOME_INTERESTED))
    ws, db = _WS(), _DB()
    _invoke(ws, db)

    leads = [r for r in db.added if getattr(r, "record_type", None) == "lead"]
    assert len(leads) == 1
    assert ws.sent[0]["lead_id"] == str(leads[0].id)
    assert ws.sent[0]["site_visit_id"] is None
    assert len(note_calls) == 1 and note_calls[0]["record_id"] == leads[0].id


def test_existing_lead_is_not_duplicated(monkeypatch):
    """If teardown already captured a lead (auto_lead_id), reuse it — no second
    lead — and write the note onto it."""
    lead_id = str(uuid.uuid4())
    state = {"auto_lead_created": True, "auto_lead_id": lead_id}
    note_calls = _patch(monkeypatch, state=state, outcome=_outcome(OUTCOME_INTERESTED))
    ws, db = _WS(), _DB()
    _invoke(ws, db)

    assert db.added == []  # no duplicate
    assert ws.sent[0]["lead_id"] == lead_id
    assert ws.sent[0]["site_visit_id"] is None
    assert len(note_calls) == 1 and str(note_calls[0]["record_id"]) == lead_id


def test_not_interested_creates_nothing(monkeypatch):
    note_calls = _patch(monkeypatch, state={}, outcome=_outcome(OUTCOME_NOT_INTERESTED))
    ws, db = _WS(), _DB()
    _invoke(ws, db)

    assert db.added == []
    assert note_calls == []  # nothing to note
    assert ws.sent[0]["lead_id"] is None
    assert ws.sent[0]["site_visit_id"] is None
