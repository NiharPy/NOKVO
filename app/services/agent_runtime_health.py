"""Operator-facing health view for the Nokvo One agent runtime.

Surface the things an SRE actually needs to look at when triage starts:

* **Retry health** — how many pending tool retries are queued, oldest
  age, and the breakdown by tool key. A growing queue means the
  downstream tool is failing and the user-facing replies will start to
  feel stuck.

* **Outcome health** — recent outcome distribution for the last N tool
  records. Spikes in ``failed`` / ``no_show`` / ``failed_followup``
  indicate either a regression in the pipeline or a real-world issue
  the agent is reflecting (e.g. assigned member never picked up).

* **Source health** — KB document inventory: approved, pending,
  rejected, plus the most-recent ingest timestamp. Helps catch stale
  knowledge that's about to surface as bad answers.

Bundled into a single :func:`build_health_report` call so the route
handler is a one-liner.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nokvo_one_tool_record import NokvoOneToolRecord
from app.models.pending_tool_retry import PendingToolRetry
from app.models.tenant_resources import TenantResources
from app.services.agent_knowledge_service import AgentKnowledgeService


# Outcome statuses we treat as "concerning" when computing the failure
# rate. A growing share of these in the last 24h is usually the first
# observable signal of a regression.
_NEGATIVE_STATUSES = frozenset(
    {"failed", "failed_followup", "no_show", "cancelled", "rejected"}
)


async def _retry_health(
    db: AsyncSession,
    *,
    organization_id: Any,
) -> dict[str, Any]:
    """Pending tool retry queue stats."""
    base = select(PendingToolRetry).where(
        PendingToolRetry.organization_id == organization_id
    ).where(PendingToolRetry.status == "pending")
    try:
        res = await db.execute(base.order_by(PendingToolRetry.created_at.asc()).limit(200))
        rows = list(res.scalars().all())
    except Exception as exc:
        return {"status": "error", "error_message": str(exc)[:200]}

    by_tool: Counter[str] = Counter()
    oldest_age_seconds = 0
    now = datetime.now(timezone.utc)
    next_due: datetime | None = None
    for row in rows:
        by_tool[str(row.tool_key or "unknown")] += 1
        created = row.created_at
        if created is not None:
            try:
                age = (now - created.astimezone(timezone.utc)).total_seconds()
            except Exception:
                age = 0
            if age > oldest_age_seconds:
                oldest_age_seconds = int(age)
        due = row.next_attempt_at
        if due is not None:
            try:
                due_utc = due.astimezone(timezone.utc)
            except Exception:
                due_utc = None
            if due_utc is not None and (next_due is None or due_utc < next_due):
                next_due = due_utc
    return {
        "pending": len(rows),
        "by_tool": dict(by_tool),
        "oldest_age_seconds": oldest_age_seconds,
        "next_due_at": next_due.isoformat() if next_due else None,
    }


async def _outcome_health(
    db: AsyncSession,
    *,
    organization_id: Any,
    window_hours: int = 24,
) -> dict[str, Any]:
    """Distribution of tool-record statuses in the recent window."""
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    try:
        stmt = (
            select(NokvoOneToolRecord.status, func.count())
            .where(NokvoOneToolRecord.organization_id == organization_id)
            .where(NokvoOneToolRecord.created_at >= since)
            .group_by(NokvoOneToolRecord.status)
        )
        res = await db.execute(stmt)
        rows = [(str(s or "unknown"), int(c or 0)) for s, c in res.all()]
    except Exception as exc:
        return {"status": "error", "error_message": str(exc)[:200]}

    total = sum(count for _, count in rows)
    by_status = {status: count for status, count in rows}
    negatives = sum(count for status, count in rows if status in _NEGATIVE_STATUSES)
    failure_rate = (negatives / total) if total else 0.0
    return {
        "window_hours": window_hours,
        "total_records": total,
        "by_status": by_status,
        "negative_count": negatives,
        "failure_rate": round(failure_rate, 4),
    }


def _source_health(tenant_res: TenantResources) -> dict[str, Any]:
    """KB document inventory + freshness."""
    documents = AgentKnowledgeService._documents(
        dict(tenant_res.provider_status or {})
    )
    if not isinstance(documents, list):
        documents = []
    counter: Counter[str] = Counter()
    most_recent: datetime | None = None
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        approval = str(doc.get("approval_status") or "unknown")
        counter[approval] += 1
        ts_raw = doc.get("updated_at") or doc.get("created_at")
        if isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                ts = None
            if ts is not None:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if most_recent is None or ts > most_recent:
                    most_recent = ts
    age_seconds: int | None = None
    if most_recent is not None:
        age_seconds = int((datetime.now(timezone.utc) - most_recent).total_seconds())
    return {
        "total_documents": sum(counter.values()),
        "by_approval_status": dict(counter),
        "most_recent_ingest_at": most_recent.isoformat() if most_recent else None,
        "most_recent_ingest_age_seconds": age_seconds,
        "policy_version": AgentKnowledgeService.policy_version(tenant_res),
    }


async def build_health_report(
    db: AsyncSession,
    tenant_res: TenantResources,
    *,
    window_hours: int = 24,
) -> dict[str, Any]:
    """Compose retry / outcome / source health into a single payload.

    Each block has the same shape (``status`` field optional, ``error_message``
    on failure) so a single error in one section doesn't crash the whole
    endpoint — operators get partial visibility rather than nothing.
    """
    organization_id = tenant_res.organization_id
    return {
        "tenant_id": str(tenant_res.tenant_id),
        "organization_id": str(organization_id) if organization_id is not None else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "retry": await _retry_health(db, organization_id=organization_id),
        "outcome": await _outcome_health(
            db,
            organization_id=organization_id,
            window_hours=window_hours,
        ),
        "source": _source_health(tenant_res),
    }


__all__ = ["build_health_report"]
