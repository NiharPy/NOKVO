"""Unit tests for the operator health report.

The builder pulls retry queue depth, recent outcome distribution, and
KB source health from three independent sources. These tests stub each
out so the builder can run without a real DB / Redis / Qdrant.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.agent_runtime_health import build_health_report


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _tenant(*, provider_status: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id="tenant-test",
        organization_id="org-test",
        provider_status=dict(provider_status or {}),
    )


class _FakeRetryRow:
    def __init__(self, *, tool_key: str, created_at: datetime, next_attempt_at: datetime):
        self.tool_key = tool_key
        self.created_at = created_at
        self.next_attempt_at = next_attempt_at


class _FakeOutcomeQuery:
    def __init__(self, rows: list[tuple[str, int]]):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeRetryQuery:
    def __init__(self, rows: list[_FakeRetryRow]):
        self._rows = rows

    def scalars(self):
        rows = self._rows

        class _Scalars:
            def all(self_inner):
                return rows

        return _Scalars()


class _FakeDB:
    def __init__(
        self,
        *,
        retry_rows: list[_FakeRetryRow] | None = None,
        outcome_rows: list[tuple[str, int]] | None = None,
        execute_should_raise: bool = False,
    ):
        self._retry_rows = retry_rows or []
        self._outcome_rows = outcome_rows or []
        self._execute_should_raise = execute_should_raise
        self.calls: list[Any] = []

    async def execute(self, stmt):
        self.calls.append(stmt)
        if self._execute_should_raise:
            raise RuntimeError("DB down")
        # Crude routing: the outcome query selects two columns, the
        # retry query selects ORM rows. We rely on the fact that the
        # outcome stmt has a ``group_by`` clause.
        text = str(stmt).lower()
        if "group by" in text:
            return _FakeOutcomeQuery(self._outcome_rows)
        return _FakeRetryQuery(self._retry_rows)


def test_health_report_aggregates_retry_outcome_and_source():
    now = datetime.now(timezone.utc)
    retry_rows = [
        _FakeRetryRow(
            tool_key="book_appointment",
            created_at=now - timedelta(minutes=10),
            next_attempt_at=now + timedelta(seconds=30),
        ),
        _FakeRetryRow(
            tool_key="book_appointment",
            created_at=now - timedelta(minutes=45),
            next_attempt_at=now + timedelta(seconds=5),
        ),
        _FakeRetryRow(
            tool_key="send_email",
            created_at=now - timedelta(minutes=2),
            next_attempt_at=now + timedelta(minutes=1),
        ),
    ]
    outcome_rows = [
        ("connected", 12),
        ("no_show", 3),
        ("failed", 2),
        ("scheduled", 8),
    ]
    documents = [
        {
            "id": "doc-1",
            "approval_status": "approved",
            "updated_at": (now - timedelta(hours=4)).isoformat(),
        },
        {
            "id": "doc-2",
            "approval_status": "pending",
            "updated_at": (now - timedelta(hours=24)).isoformat(),
        },
        {
            "id": "doc-3",
            "approval_status": "approved",
            "created_at": (now - timedelta(days=2)).isoformat(),
        },
    ]
    tenant = _tenant(
        provider_status={
            "agent_knowledge_documents": documents,
            "agent_policy_version": "pv_test",
        }
    )
    db = _FakeDB(retry_rows=retry_rows, outcome_rows=outcome_rows)

    report = _run(build_health_report(db, tenant, window_hours=12))

    assert report["tenant_id"] == "tenant-test"
    # Retry block
    assert report["retry"]["pending"] == 3
    assert report["retry"]["by_tool"] == {"book_appointment": 2, "send_email": 1}
    # Oldest age is from the 45-minute-old row.
    assert report["retry"]["oldest_age_seconds"] >= 45 * 60
    assert report["retry"]["next_due_at"] is not None
    # Outcome block
    assert report["outcome"]["window_hours"] == 12
    assert report["outcome"]["total_records"] == 25
    assert report["outcome"]["by_status"] == {
        "connected": 12,
        "no_show": 3,
        "failed": 2,
        "scheduled": 8,
    }
    # no_show + failed = 5 out of 25 = 0.2
    assert report["outcome"]["negative_count"] == 5
    assert report["outcome"]["failure_rate"] == 0.2
    # Source block
    assert report["source"]["total_documents"] == 3
    assert report["source"]["by_approval_status"] == {"approved": 2, "pending": 1}
    assert report["source"]["most_recent_ingest_at"] is not None
    assert report["source"]["policy_version"] == "pv_test"


def test_health_report_partial_failure_does_not_crash():
    """A DB error in the retry block should not blow up the whole
    endpoint — operators should still see source info."""
    tenant = _tenant(
        provider_status={
            "agent_knowledge_documents": [
                {"id": "doc", "approval_status": "approved"}
            ],
        }
    )
    db = _FakeDB(execute_should_raise=True)
    report = _run(build_health_report(db, tenant))
    assert report["retry"].get("status") == "error"
    assert report["outcome"].get("status") == "error"
    # Source health doesn't touch the DB, so it survives.
    assert report["source"]["total_documents"] == 1


def test_source_health_handles_missing_documents_block():
    tenant = _tenant(provider_status={})
    db = _FakeDB(retry_rows=[], outcome_rows=[])
    report = _run(build_health_report(db, tenant))
    assert report["source"]["total_documents"] == 0
    assert report["source"]["most_recent_ingest_at"] is None
    assert report["source"]["most_recent_ingest_age_seconds"] is None


def test_outcome_failure_rate_is_zero_when_no_records():
    tenant = _tenant()
    db = _FakeDB(retry_rows=[], outcome_rows=[])
    report = _run(build_health_report(db, tenant))
    assert report["outcome"]["total_records"] == 0
    assert report["outcome"]["failure_rate"] == 0.0
    assert report["retry"]["pending"] == 0
    assert report["retry"]["oldest_age_seconds"] == 0
