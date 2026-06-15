"""Transcript service — store (best-effort upsert), .txt render, rolling purge.

No real DB: a fake session captures the SQL so we can assert the upsert + the
org-scoped retention DELETE without a Postgres round-trip.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from app.services.transcript_service import RETENTION, TranscriptService, _normalize_turns


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Result:
    def __init__(self, rowcount=0):
        self.rowcount = rowcount


class _FakeDB:
    def __init__(self):
        self.executed = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _Result(3)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


# ── turn normalisation ──────────────────────────────────────────────────────


def test_normalize_turns_drops_empty_and_collapses_whitespace():
    turns = _normalize_turns([
        {"role": "assistant", "content": "  hi   there  "},
        {"role": "user", "content": ""},          # empty → dropped
        {"content": "no role"},                    # missing role → "turn"
        None,                                       # junk → skipped
    ])
    assert turns == [
        {"role": "assistant", "content": "hi there"},
        {"role": "turn", "content": "no role"},
    ]


# ── .txt rendering ──────────────────────────────────────────────────────────


def test_render_txt_has_header_and_role_labels():
    txt = TranscriptService.render_txt(
        turns=[
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "Hi there"},
            {"role": "system", "content": ""},      # empty → not rendered
        ],
        started_at="2026-06-15T10:00:00+00:00",
        kind="inbound",
        duration_seconds=42,
        caller_phone="+919999999999",
    )
    assert "NOKVO call transcript" in txt
    assert "Type: inbound" in txt
    assert "Duration: 42s" in txt
    assert "+919999999999" in txt
    assert "Agent: Hello" in txt          # assistant → Agent
    assert "Caller: Hi there" in txt      # user → Caller
    assert "System:" not in txt           # empty content dropped


# ── store (best-effort upsert) ──────────────────────────────────────────────


def test_store_upserts_and_commits():
    db = _FakeDB()
    ok = _run(TranscriptService.store(
        db, organization_id="o", tenant_id="t", call_id="c1",
        history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        duration_seconds=10, kind="inbound", caller_phone="+91",
    ))
    assert ok is True
    assert db.committed
    assert len(db.executed) == 1
    sql = str(db.executed[0]).upper()
    assert "CALL_TRANSCRIPTS" in sql
    assert "ON CONFLICT" in sql           # idempotent upsert on call_id


def test_store_noop_when_no_turns():
    db = _FakeDB()
    ok = _run(TranscriptService.store(
        db, organization_id="o", tenant_id="t", call_id="c", history=[],
    ))
    assert ok is False
    assert db.executed == []               # nothing written


def test_store_best_effort_on_error():
    class _BoomDB(_FakeDB):
        async def execute(self, stmt):
            raise RuntimeError("db down")

    db = _BoomDB()
    ok = _run(TranscriptService.store(
        db, organization_id="o", tenant_id="t", call_id="c",
        history=[{"role": "user", "content": "hi"}],
    ))
    assert ok is False                     # never raises out
    assert db.rolled_back


# ── rolling retention purge ─────────────────────────────────────────────────


def test_retention_window_is_one_month():
    assert RETENTION == timedelta(days=30)


def test_purge_expired_filters_started_at_and_commits():
    db = _FakeDB()
    n = _run(TranscriptService.purge_expired(db))
    assert n == 3
    assert db.committed
    sql = str(db.executed[0])
    assert "call_transcripts" in sql
    assert "started_at" in sql             # deletes by age
    assert "organization_id" not in sql    # all-orgs sweep → no org filter


def test_purge_expired_scopes_to_org_when_given():
    db = _FakeDB()
    _run(TranscriptService.purge_expired(db, organization_id="org-1"))
    sql = str(db.executed[0])
    assert "started_at" in sql
    assert "organization_id" in sql        # org-scoped (lazy purge on the list endpoint)
