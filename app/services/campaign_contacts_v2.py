"""Scalable per-row campaign-contact storage (the 1M-ready path).

Replaces the O(n) JSONB blob on ``outbound_campaigns.contacts`` with indexed rows
on ``outbound_campaign_contacts``:

  * INGEST  — async, streamed, **COPY into a staging temp table → INSERT … SELECT …
              ON CONFLICT DO NOTHING** (dedupe in the DB, bounded memory). Uses a
              raw asyncpg connection for the COPY hot path.
  * DIAL    — a two-phase claim: a tiny ``pg_advisory_xact_lock`` transaction that
              computes the concurrency headroom and marks up to ``k`` pending rows
              ``dialing`` (``UPDATE … FOR UPDATE SKIP LOCKED``) then COMMITS to
              release the lock. NO network I/O (DND / Plivo) is done under the lock.
  * UPDATE  — status webhooks are O(1) single-row updates keyed on ``call_link_id``;
              campaign aggregates are atomic counter increments.
  * READ    — GROUP BY summary + keyset pagination + a streaming CSV generator.

Gated by ``settings.CAMPAIGN_CONTACTS_V2`` — the legacy blob path stays intact
until every reader (endpoints + frontend) is migrated.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterable

import asyncpg
from sqlalchemy import text

from app.core.config import settings
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# UI bucket → SQL predicate over a contact row (mirrors the old categorizeContacts).
BUCKET_SQL: dict[str, str] = {
    "qualified": "qualified = true",
    "not_interested": "status = 'completed' AND qualified = false",
    "no_pickup": "status IN ('no_answer', 'failed', 'dnd_dropped')",
    "pending": "status IN ('pending', 'dialing', 'ringing', 'answered')",
}
_LIVE_STATUSES = ("dialing", "ringing", "answered")


# ── raw asyncpg (COPY hot path) ──────────────────────────────────────────────
async def _asyncpg_connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=settings.POSTGRES_SERVER,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        database=settings.POSTGRES_DB,
        ssl="require",
    )


async def ingest_rows(campaign_id: uuid.UUID, rows: Iterable[dict[str, str]]) -> int:
    """COPY ``rows`` ({phone,name}) into ``outbound_campaign_contacts`` for a NEW
    campaign, chunked + deduped via a staging temp table. Returns rows inserted
    (post-dedupe). Bounded memory: only one chunk is held at a time."""
    chunk_size = max(1000, int(settings.CAMPAIGN_INGEST_CHUNK or 10000))
    conn = await _asyncpg_connect()
    inserted = 0
    try:
        buf: list[tuple] = []

        async def _flush(batch: list[tuple]) -> int:
            if not batch:
                return 0
            async with conn.transaction():
                await conn.execute(
                    "CREATE TEMP TABLE _stg_contacts "
                    "(id uuid, campaign_id uuid, phone text, name text, call_link_id text) "
                    "ON COMMIT DROP"
                )
                await conn.copy_records_to_table(
                    "_stg_contacts", records=batch,
                    columns=["id", "campaign_id", "phone", "name", "call_link_id"],
                )
                # ON CONFLICT dedupe on (campaign_id, phone). Cross-chunk-safe.
                res = await conn.execute(
                    "INSERT INTO outbound_campaign_contacts "
                    "(id, campaign_id, phone, name, call_link_id, status, attempt, qualified, result, snapshot) "
                    "SELECT id, campaign_id, phone, name, call_link_id, 'pending', 0, false, '{}'::jsonb, '{}'::jsonb "
                    "FROM _stg_contacts "
                    "ON CONFLICT (campaign_id, phone) DO NOTHING"
                )
            # asyncpg returns e.g. "INSERT 0 <n>"
            try:
                return int(res.split()[-1])
            except (ValueError, IndexError):
                return 0

        for r in rows:
            phone = (r.get("phone") or "").strip()
            if not phone:
                continue
            buf.append((uuid.uuid4(), campaign_id, phone, (r.get("name") or None), str(uuid.uuid4())))
            if len(buf) >= chunk_size:
                inserted += await _flush(buf)
                buf = []
        inserted += await _flush(buf)
    finally:
        await conn.close()
    return inserted


# ── dial claim (advisory-locked, no network I/O under the lock) ──────────────
async def claim_pending(campaign_id: uuid.UUID, cap: int, *, max_rows: int | None = None) -> list[dict[str, Any]]:
    """Phase 1: under a short ``pg_advisory_xact_lock`` transaction, compute the
    concurrency headroom and atomically mark up to ``k`` pending rows ``dialing``.
    Commits (releasing the lock) before returning. Returns [{id, phone,
    call_link_id}] for the caller to DND-scrub + place OUTSIDE any lock.

    ``max_rows`` further bounds ``k`` (e.g. the remaining daily-cap budget)."""
    async with AsyncSessionLocal() as db:
        async with db.begin():  # single short txn; advisory lock auto-releases on commit
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:c))"), {"c": str(campaign_id)}
            )
            live = (await db.execute(
                text("SELECT count(*) FROM outbound_campaign_contacts "
                     "WHERE campaign_id = :c AND status = ANY(:s)"),
                {"c": str(campaign_id), "s": list(_LIVE_STATUSES)},
            )).scalar_one()
            k = max(0, int(cap) - int(live))
            if max_rows is not None:
                k = min(k, max(0, int(max_rows)))
            if k <= 0:
                return []
            rows = (await db.execute(
                text(
                    "UPDATE outbound_campaign_contacts SET status='dialing', attempt=attempt+1, "
                    "updated_at=now() WHERE id IN ("
                    "  SELECT id FROM outbound_campaign_contacts "
                    "  WHERE campaign_id = :c AND status = 'pending' "
                    "  ORDER BY created_at LIMIT :k FOR UPDATE SKIP LOCKED"
                    ") RETURNING id, phone, call_link_id"
                ),
                {"c": str(campaign_id), "k": k},
            )).mappings().all()
            return [dict(r) for r in rows]


async def mark_contact(contact_id: uuid.UUID, status: str, **fields: Any) -> None:
    """O(1) status transition for a claimed row (used after DND/Plivo outside the
    lock: 'dnd_dropped', 'ringing', or back to 'pending'/'failed' on error)."""
    sets = ["status = :status", "updated_at = now()"]
    params: dict[str, Any] = {"status": status, "id": str(contact_id)}
    for col in ("call_id",):
        if col in fields:
            sets.append(f"{col} = :{col}")
            params[col] = fields[col]
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(f"UPDATE outbound_campaign_contacts SET {', '.join(sets)} WHERE id = :id"), params
        )
        await db.commit()


async def update_status_by_link(db, call_link_id: str, status: str, **fields: Any) -> bool:
    """O(1) webhook update keyed on the unique ``call_link_id``. Idempotent."""
    sets = ["status = :status", "updated_at = now()"]
    params: dict[str, Any] = {"status": status, "clid": call_link_id}
    for col in ("call_id", "answered_at", "duration_s", "lead_score", "qualified"):
        if col in fields:
            sets.append(f"{col} = :{col}")
            params[col] = fields[col]
    if "result" in fields:
        # asyncpg needs a JSON string + explicit cast for a JSONB column.
        sets.append("result = CAST(:result AS jsonb)")
        params["result"] = json.dumps(fields["result"] or {})
    res = await db.execute(
        text(f"UPDATE outbound_campaign_contacts SET {', '.join(sets)} WHERE call_link_id = :clid"),
        params,
    )
    return (res.rowcount or 0) > 0


async def maybe_complete(db, campaign_id: uuid.UUID) -> bool:
    """Flip a drained V2 campaign ``running`` → ``completed``: no rows left
    pending and none on a live line. One indexed count + a status-guarded UPDATE
    (never touches cancelled/ingesting), so it's cheap and idempotent. Without
    this a finished V2 campaign stayed ``running`` forever — which also held the
    tenant's one-campaign-at-a-time slot. An append (add-contacts) resumes a
    completed campaign back to ``running`` after its ingest."""
    remaining = (await db.execute(
        text("SELECT count(*) FROM outbound_campaign_contacts "
             "WHERE campaign_id = :c AND status = ANY(:s)"),
        {"c": str(campaign_id), "s": ["pending", *_LIVE_STATUSES]},
    )).scalar_one()
    if int(remaining) > 0:
        return False
    res = await db.execute(
        text("UPDATE outbound_campaigns SET status = 'completed', completed_at = now() "
             "WHERE id = :c AND status = 'running'"),
        {"c": str(campaign_id)},
    )
    await db.commit()
    return (res.rowcount or 0) > 0


async def bump_campaign_counter(db, campaign_id: uuid.UUID, field: str, n: int = 1) -> None:
    """Atomic counter increment (answered_count / failed_count / contacts_dialed)."""
    if field not in ("answered_count", "failed_count", "contacts_dialed", "total_count"):
        raise ValueError(f"illegal counter {field!r}")
    await db.execute(
        text(f"UPDATE outbound_campaigns SET {field} = {field} + :n WHERE id = :c"),
        {"n": n, "c": str(campaign_id)},
    )


# ── reads: summary + keyset pagination + streaming CSV ───────────────────────
_SUMMARY_TTL_S = 5  # dashboard tiles may be a few seconds stale; avoids re-scanning 1M


async def summary(db, campaign_id: uuid.UUID) -> dict[str, int]:
    """Per-status counts + qualified via one indexed GROUP BY. Cached in Redis for
    a few seconds so the dashboard never re-scans a large table on every poll.
    Best-effort cache — a Redis hiccup just recomputes."""
    key = f"occ:summary:{campaign_id}"
    try:
        from app.services.agent_session_store import AgentSessionStore

        cached = await AgentSessionStore.client().get(key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    out = await _summary_uncached(db, campaign_id)
    try:
        from app.services.agent_session_store import AgentSessionStore

        await AgentSessionStore.client().setex(key, _SUMMARY_TTL_S, json.dumps(out))
    except Exception:
        pass
    return out


async def _summary_uncached(db, campaign_id: uuid.UUID) -> dict[str, int]:
    rows = (await db.execute(
        text("SELECT status, count(*) n FROM outbound_campaign_contacts "
             "WHERE campaign_id = :c GROUP BY status"),
        {"c": str(campaign_id)},
    )).all()
    by_status = {r[0]: int(r[1]) for r in rows}
    qualified = (await db.execute(
        text("SELECT count(*) FROM outbound_campaign_contacts "
             "WHERE campaign_id = :c AND qualified"),
        {"c": str(campaign_id)},
    )).scalar_one()
    total = sum(by_status.values())
    return {
        "total": total,
        "qualified": int(qualified),
        "not_interested": by_status.get("completed", 0) - int(qualified),
        "no_pickup": by_status.get("no_answer", 0) + by_status.get("failed", 0) + by_status.get("dnd_dropped", 0),
        "pending": total - by_status.get("completed", 0) - by_status.get("no_answer", 0)
                   - by_status.get("failed", 0) - by_status.get("dnd_dropped", 0),
        "by_status": by_status,
    }


async def page_contacts(db, campaign_id: uuid.UUID, bucket: str, *, cursor: str | None = None,
                        limit: int = 100) -> dict[str, Any]:
    """Keyset pagination over a bucket. ``cursor`` is the last row id seen."""
    pred = BUCKET_SQL.get(bucket, BUCKET_SQL["pending"])
    limit = max(1, min(500, int(limit)))
    params: dict[str, Any] = {"c": str(campaign_id), "lim": limit + 1}
    cur = ""
    if cursor:
        cur = " AND id > :cursor"
        params["cursor"] = cursor
    rows = (await db.execute(
        text(f"SELECT id, phone, name, status, lead_score, qualified, result "
             f"FROM outbound_campaign_contacts "
             f"WHERE campaign_id = :c AND ({pred}){cur} ORDER BY id LIMIT :lim"),
        params,
    )).mappings().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "rows": [dict(r) for r in rows],
        "next_cursor": str(rows[-1]["id"]) if (has_more and rows) else None,
    }


# ── APEX claim pool (qualified leads, on the row) ────────────────────────────
def _lead_row_from_rec(rec: dict) -> dict:
    """Map a joined contact+campaign row to the API's lead-row shape."""
    cfg = rec.get("agent_config") or {}
    q = (cfg.get("questionnaire") or {})
    has_q = bool(q.get("questions"))
    result = rec.get("result") or {}
    if isinstance(result, str):
        result = json.loads(result or "{}")
    return {
        "campaign_id": str(rec["campaign_id"]),
        "campaign_name": rec.get("campaign_name"),
        "call_link_id": rec.get("call_link_id"),
        "name": rec.get("name") or rec.get("phone"),
        "phone": rec.get("phone"),
        "lead_score": rec.get("lead_score"),
        "max_score": cfg.get("max_score") or (len(q.get("questions") or []) if has_q else None),
        "score_breakdown": result.get("score_breakdown") or [],
        "lead_score_reason": result.get("lead_score_reason") or result.get("interest_reason"),
        "call_note": result.get("call_note"),
        "claim_status": result.get("claim_status"),
        "claimed_by_name": result.get("claimed_by_name"),
        "claimed_at": rec["claimed_at"].isoformat() if rec.get("claimed_at") else None,
    }


_POOL_COLS = ("occ.campaign_id, c.name AS campaign_name, c.agent_config, occ.call_link_id, "
              "occ.phone, occ.name, occ.lead_score, occ.result, occ.claimed_at")


async def qualified_pool(db, tenant_id: str, *, claimed_by: str | None = None, limit: int = 500) -> list[dict]:
    """Qualified rows across the tenant's campaigns. ``claimed_by=None`` → the
    UNCLAIMED pool; a user id → that member's claimed leads. Indexed by the partial
    ``qualified`` index."""
    where = "c.tenant_id = :t AND occ.qualified"
    params: dict[str, Any] = {"t": tenant_id, "lim": max(1, min(1000, int(limit)))}
    if claimed_by is None:
        where += " AND occ.claimed_by IS NULL"
    else:
        where += " AND occ.claimed_by = :u"
        params["u"] = str(claimed_by)
    rows = (await db.execute(
        text(f"SELECT {_POOL_COLS} FROM outbound_campaign_contacts occ "
             f"JOIN outbound_campaigns c ON c.id = occ.campaign_id "
             f"WHERE {where} ORDER BY occ.campaign_id, occ.id LIMIT :lim"),
        params,
    )).mappings().all()
    return [_lead_row_from_rec(dict(r)) for r in rows]


async def claim_lead(db, tenant_id: str, campaign_id: uuid.UUID, call_link_id: str,
                     user_id: str, user_name: str | None) -> dict | None:
    """Atomically claim an unclaimed qualified row (first-come). Returns the lead
    row, or ``None`` if there's no matching V2 row (caller falls back to the blob)
    or it's already claimed (caller maps to 409)."""
    meta = json.dumps({"claimed_by_name": user_name, "claim_status": "claimed"})
    rows = (await db.execute(
        text(
            "UPDATE outbound_campaign_contacts occ SET claimed_by = :u, claimed_at = now(), "
            "result = result || CAST(:meta AS jsonb), updated_at = now() "
            "FROM outbound_campaigns c "
            "WHERE occ.campaign_id = c.id AND c.tenant_id = :t AND occ.campaign_id = :cid "
            "AND occ.call_link_id = :clid AND occ.qualified AND occ.claimed_by IS NULL "
            f"RETURNING {_POOL_COLS}"
        ),
        {"u": str(user_id), "meta": meta, "t": tenant_id, "cid": str(campaign_id), "clid": call_link_id},
    )).mappings().all()
    if rows:
        await db.commit()
        return _lead_row_from_rec(dict(rows[0]))
    return None


async def set_lead_status(db, tenant_id: str, campaign_id: uuid.UUID, call_link_id: str,
                          user_id: str, new_status: str) -> dict | None | str:
    """Update a claimed lead's working status. Returns the lead row on success,
    ``None`` if no V2 row matched (fall back to blob), or ``"forbidden"`` if it's
    claimed by someone else."""
    owner = (await db.execute(
        text("SELECT occ.claimed_by FROM outbound_campaign_contacts occ "
             "JOIN outbound_campaigns c ON c.id = occ.campaign_id "
             "WHERE c.tenant_id = :t AND occ.campaign_id = :cid AND occ.call_link_id = :clid"),
        {"t": tenant_id, "cid": str(campaign_id), "clid": call_link_id},
    )).scalar_one_or_none()
    if owner is None:
        return None
    if str(owner) != str(user_id):
        return "forbidden"
    rows = (await db.execute(
        text(
            "UPDATE outbound_campaign_contacts occ SET "
            "result = jsonb_set(result, '{claim_status}', to_jsonb(CAST(:s AS text))), updated_at = now() "
            "FROM outbound_campaigns c WHERE occ.campaign_id = c.id AND c.tenant_id = :t "
            "AND occ.campaign_id = :cid AND occ.call_link_id = :clid "
            f"RETURNING {_POOL_COLS}"
        ),
        {"s": new_status, "t": tenant_id, "cid": str(campaign_id), "clid": call_link_id},
    )).mappings().all()
    await db.commit()
    return _lead_row_from_rec(dict(rows[0])) if rows else None


async def iter_csv_rows(db, campaign_id: uuid.UUID, bucket: str) -> AsyncIterator[tuple[str, str]]:
    """Stream (phone, name) for a bucket, keyset-paged so 1M never lands in RAM."""
    pred = BUCKET_SQL.get(bucket, BUCKET_SQL["pending"])
    cursor = ""
    while True:
        params = {"c": str(campaign_id)}
        cur = ""
        if cursor:
            cur = " AND id > :cursor"
            params["cursor"] = cursor
        batch = (await db.execute(
            text(f"SELECT id, phone, name FROM outbound_campaign_contacts "
                 f"WHERE campaign_id = :c AND ({pred}){cur} ORDER BY id LIMIT 1000"),
            params,
        )).mappings().all()
        if not batch:
            return
        for r in batch:
            yield (r["phone"] or "", r["name"] or "")
        cursor = str(batch[-1]["id"])
