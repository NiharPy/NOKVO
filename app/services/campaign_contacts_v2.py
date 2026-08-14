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
import math
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterable

import asyncpg
from sqlalchemy import text

from app.core.config import settings
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# UI bucket → SQL predicate over a contact row (mirrors the old categorizeContacts).
# "busy" = connected, didn't qualify, but ASKED TO BE CALLED BACK (the post-call
# classifier stamps result.callback_requested) — a re-dialable outcome, split out
# of not_interested so true rejections stay a clean list and the busy pile can be
# re-run from the campaign card.
BUCKET_SQL: dict[str, str] = {
    "qualified": "qualified = true",
    "not_interested": (
        "status = 'completed' AND qualified = false "
        "AND COALESCE(result->>'callback_requested', '') <> 'true'"
    ),
    "busy": (
        "status = 'completed' AND qualified = false "
        "AND result->>'callback_requested' = 'true'"
    ),
    "no_pickup": "status IN ('no_answer', 'failed', 'dnd_dropped')",
    "pending": "status IN ('pending', 'dialing', 'ringing', 'answered')",
    # Call Logs: every contact a call was actually PLACED for (any outcome).
    "dialed": "call_id IS NOT NULL",
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


async def ingest_api_rows(
    db, campaign_id: uuid.UUID, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    """CRM/API ingest: small batches (≤ hundreds) of already-normalized
    ``{phone, name, external_id, meta}`` rows → pending contact rows. One
    multi-VALUES INSERT … ON CONFLICT DO NOTHING on ``(campaign_id, phone)``,
    so a CRM re-firing its webhook is a harmless no-op (idempotent ingest).
    Returns ``(inserted, duplicates)``. NOT the CSV COPY path — that one is
    for 1M-row files and needs a raw asyncpg connection."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.outgoing_lead import OutboundCampaignContact

    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        phone = (r.get("phone") or "").strip()
        if not phone or phone in seen:
            continue
        seen.add(phone)
        snapshot: dict[str, Any] = {"source": "api"}
        if r.get("meta"):
            snapshot["meta"] = r["meta"]
        values.append({
            "id": uuid.uuid4(),
            "campaign_id": campaign_id,
            "phone": phone,
            "name": (r.get("name") or None),
            "external_id": (r.get("external_id") or None),
            "call_link_id": str(uuid.uuid4()),
            "status": "pending",
            "attempt": 0,
            "qualified": False,
            "result": {},
            "snapshot": snapshot,
        })
    if not values:
        return 0, 0
    stmt = pg_insert(OutboundCampaignContact).values(values).on_conflict_do_nothing(
        constraint="uq_outbound_campaign_contact_phone"
    )
    res = await db.execute(stmt)
    await db.commit()
    inserted = int(res.rowcount or 0)
    return inserted, len(values) - inserted


# ── dial claim (advisory-locked, no network I/O under the lock) ──────────────
async def _bump_dialed_today(db, campaign_id: uuid.UUID, today: str, n: int) -> None:
    """Atomically advance the per-day placement counter on ``agent_config``,
    resetting on an IST date rollover. A targeted ``jsonb_set`` — NEVER a
    whole-blob write, which raced concurrent campaign edits / other refills and
    lost updates. Self-sufficient: creates ``call_window``/``dialed_today`` if a
    concurrent whole-blob write clobbered them (jsonb_set can't create
    intermediate paths, so the outer set floors call_window to {})."""
    await db.execute(
        text(
            "UPDATE outbound_campaigns SET agent_config = jsonb_set("
            " jsonb_set(agent_config, '{call_window}',"
            "           COALESCE(agent_config->'call_window', '{}'::jsonb), true),"
            " '{call_window,dialed_today}',"
            " CASE WHEN agent_config#>>'{call_window,dialed_today,date}' = :today"
            "      THEN jsonb_build_object('date', :today, 'count',"
            "           COALESCE((agent_config#>>'{call_window,dialed_today,count}')::int, 0) + :n)"
            "      ELSE jsonb_build_object('date', :today, 'count', :n) END, true) "
            "WHERE id = :c"
        ),
        {"today": today, "n": int(n), "c": str(campaign_id)},
    )


async def refund_dialed_today(db, campaign_id: uuid.UUID, today: str, n: int) -> None:
    """Give back daily-cap budget for claims that never became placements
    (DND-dropped / failed at placement). The claim charges the counter up front
    (inside its locked txn, so concurrent refills can't overshoot the cap); this
    refunds the difference afterwards. Clamped at 0; only today's counter (a
    crashed refund under-counts tomorrow never, undershoots today at worst —
    the conservative failure). Caller commits."""
    if n <= 0:
        return
    await db.execute(
        text(
            "UPDATE outbound_campaigns SET agent_config = jsonb_set(agent_config,"
            " '{call_window,dialed_today,count}',"
            " to_jsonb(GREATEST(COALESCE((agent_config#>>'{call_window,dialed_today,count}')::int, 0) - :n, 0))) "
            "WHERE id = :c AND agent_config#>>'{call_window,dialed_today,date}' = :today"
        ),
        {"n": int(n), "c": str(campaign_id), "today": today},
    )


async def claim_pending(
    campaign_id: uuid.UUID,
    cap: int,
    *,
    daily_cap: int | None = None,
    today: str = "",
    ring_multiplier: float = 1.0,
    max_ring_ahead: int | None = None,
) -> list[dict[str, Any]]:
    """Phase 1: under a short ``pg_advisory_xact_lock`` transaction, verify the
    campaign is still RUNNING, compute the concurrency headroom AND the daily-cap
    budget from a FRESH ``dialed_today`` read, atomically mark up to ``k`` pending
    rows ``dialing``, and charge ``dialed_today`` by the claimed count — all in
    the same locked txn, so concurrent refills (webhook + ticker + other replicas)
    can never overshoot ``daily_cap``. Commits (releasing the lock) before
    returning. Returns [{id, phone, call_link_id}] for the caller to DND-scrub +
    place OUTSIDE any lock; the caller REFUNDS the counter for claims that didn't
    become placements (:func:`refund_dialed_today`).

    Two live populations, bounded separately — they are not the same thing:

    * ``cap`` is the CONVERSATION cap (an APEX org's plan concurrency). Only rows
      in ``answered`` hold a conversation slot, so only those count against it.
      It is the promise sold to the customer and is never exceeded.
    * ringing rows (``dialing``/``ringing``) are calls placed in the hope they
      connect. Their ceiling is ``free_slots × ring_multiplier``, clamped by
      ``max_ring_ahead``.

    ``ring_multiplier`` is "lines to ring per free conversation slot". At the
    default 1.0 every ringing call has a slot reserved for it, so a connect can
    never be abandoned. The pacer passes ``1 / answer_rate`` so that the EXPECTED
    number of simultaneous answers is the plan's concurrency — throughput without
    abandonment.

    The bug this replaces: a single ``live`` count of dialing+ringing+answered
    against a hardcoded 5, while the plan sold 1. The dialer placed five calls,
    the media WS admitted one, and the other four humans were hung up on the
    moment they said hello.

    The status check kills the cancel-vs-refill race: a cancel that committed
    after the caller loaded its campaign snapshot wins here (``cancel_campaign``
    takes the same advisory key), so a stale refill claims nothing."""
    conv_cap = max(0, int(cap))
    try:
        multiplier = max(1.0, float(ring_multiplier))
    except (TypeError, ValueError):
        multiplier = 1.0
    async with AsyncSessionLocal() as db:
        async with db.begin():  # single short txn; advisory lock auto-releases on commit
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:c))"), {"c": str(campaign_id)}
            )
            row = (await db.execute(
                text(
                    "SELECT status, agent_config#>>'{call_window,dialed_today,date}', "
                    "agent_config#>>'{call_window,dialed_today,count}' "
                    "FROM outbound_campaigns WHERE id = :c"
                ),
                {"c": str(campaign_id)},
            )).first()
            if row is None or str(row[0]) != "running":
                return []  # cancelled/completed since the caller's snapshot — claim nothing
            # One scan, two counts: conversations in progress vs calls still ringing.
            live_row = (await db.execute(
                text(
                    "SELECT "
                    "  count(*) FILTER (WHERE status = 'answered') AS in_conversation, "
                    "  count(*) FILTER (WHERE status IN ('dialing', 'ringing')) AS ringing "
                    "FROM outbound_campaign_contacts "
                    "WHERE campaign_id = :c AND status = ANY(:s)"
                ),
                {"c": str(campaign_id), "s": list(_LIVE_STATUSES)},
            )).first()
            in_conversation = int(live_row[0] or 0)
            ringing = int(live_row[1] or 0)
            free_slots = max(0, conv_cap - in_conversation)
            # How many lines may be ringing right now. At multiplier 1.0 this is
            # exactly the free slot count, so every connect has somewhere to land.
            ring_target = int(math.ceil(free_slots * multiplier))
            if max_ring_ahead is not None:
                ring_target = min(ring_target, max(0, int(max_ring_ahead)))
            k = max(0, ring_target - ringing)
            if daily_cap is not None and daily_cap > 0:
                try:
                    today_count = int(row[2]) if (row[1] == today and row[2] is not None) else 0
                except (TypeError, ValueError):
                    today_count = 0  # junk counter → same tolerance as _dialed_today_count
                k = min(k, max(0, int(daily_cap) - today_count))
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
            claimed = [dict(r) for r in rows]
            # Charge the daily counter for the whole claim NOW, inside the lock —
            # the caller refunds whatever doesn't get placed.
            if claimed and daily_cap is not None and daily_cap > 0 and today:
                await _bump_dialed_today(db, campaign_id, today, len(claimed))
            return claimed


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


async def stamp_answered_by_link(db, call_link_id: str, ts: datetime) -> uuid.UUID | None:
    """GUARDED answered stamp: transitions only a live pre-answer row. Returns the
    campaign_id when THIS call did the transition, else None — so the caller bumps
    ``answered_count`` exactly once. A duplicate/late answered event matches
    nothing (idempotent) and, unlike the old unguarded write, can never re-open a
    ``completed`` row and re-bump the counter. Caller commits."""
    # Which renditions this call speaks. Both are pure crc32(call_link_id), so
    # they are recomputed here rather than threaded down through the voice stream
    # — and they come from the SAME helpers the speech paths call, so the recorded
    # variant cannot drift from the audio the callee actually heard. Recorded on
    # answer because an unanswered call spoke nothing worth attributing.
    from app.services.apex_micro_acks import opener_variant_for_call, tts_variant_for_call

    row = (await db.execute(
        text(
            "UPDATE outbound_campaign_contacts "
            "SET status = 'answered', answered_at = COALESCE(answered_at, CAST(:ts AS timestamptz)), "
            "tts_variant = :tts_v, opener_variant = :op_v, updated_at = now() "
            "WHERE call_link_id = :clid AND status IN ('dialing', 'ringing') "
            "RETURNING campaign_id"
        ),
        {
            "clid": call_link_id,
            "ts": ts.isoformat(),
            "tts_v": tts_variant_for_call(call_link_id),
            "op_v": opener_variant_for_call(call_link_id),
        },
    )).first()
    return row[0] if row else None


async def finalize_terminal_by_link(
    db,
    call_link_id: str,
    final: str,
    duration_s: float | None = None,
    hangup_cause: str | None = None,
) -> tuple[str, uuid.UUID] | None:
    """GUARDED terminal transition, one statement: an ``answered`` row completes;
    a still-live pre-answer row lands on ``final`` (failed/no_answer). Race-safe
    under READ COMMITTED — the SET's CASE reads the pre-update status, and a
    concurrent duplicate's WHERE is re-checked against the winner's committed
    tuple (now terminal) so the loser matches nothing. (NOT a self-join UPDATE …
    FROM: EvalPlanQual re-reads only the target tuple, the FROM side keeps the
    stale snapshot — a self-join loser would double-transition.) Replaces the
    SELECT-then-UPDATE that let concurrent Plivo retries double-bump counters.
    Returns (new_status, campaign_id) when this call transitioned the row, else
    None (already terminal / unknown). Caller commits."""
    row = (await db.execute(
        text(
            "UPDATE outbound_campaign_contacts "
            "SET status = CASE WHEN status = 'answered' THEN 'completed' ELSE :final END, "
            "duration_s = COALESCE(CAST(:dur AS numeric), duration_s), "
            # The carrier's verbatim cause. Kept because 'no_answer' collapses four
            # problems with four different fixes — invalid number, phone off, call
            # rejected, genuinely nobody home — and the retry policy needs to tell
            # them apart before it can decide what is worth dialing again.
            "hangup_cause = COALESCE(:cause, hangup_cause), updated_at = now() "
            "WHERE call_link_id = :clid AND status IN ('dialing', 'ringing', 'answered') "
            "RETURNING status, campaign_id"
        ),
        {"final": final, "dur": duration_s, "cause": hangup_cause, "clid": call_link_id},
    )).first()
    return (str(row[0]), row[1]) if row else None


def _blob_row_status(ct: dict) -> str:
    """Map a legacy blob contact's state onto the V2 status vocabulary.
    Answered contacts are never re-dialed; in-flight placements stay live (the
    stale-row reaper resolves them if their webhook never lands)."""
    s = str(ct.get("status") or "pending")
    ended = bool(ct.get("ended"))
    if s == "answered":
        return "completed" if ended else "answered"
    if s in ("completed", "no_answer", "failed", "dnd_dropped", "pending"):
        return s
    if s in ("calling", "dialing", "ringing"):
        return "no_answer" if ended else "ringing"
    return "no_answer" if ct.get("call_id") else "pending"


async def migrate_blob_campaign(db, campaign) -> int:
    """ONE-WAY upgrade of a legacy blob campaign to the per-row store, so it can
    accept CSV appends / rerun / claims through the V2 paths. Copies every blob
    contact into ``outbound_campaign_contacts`` (statuses mapped, call_link_id +
    claim state + score preserved, extras parked in ``result``), then NULLs the
    blob — the campaign reads as V2 everywhere from here on. Idempotent-ish:
    ON CONFLICT DO NOTHING skips contacts that already have a row (either
    constraint), and an empty blob just NULLs. Returns rows inserted."""
    from app.services.outbound_campaign_service import _canonical_phone

    blob = list(campaign.contacts or [])
    inserted = 0
    _row_cols = ("phone", "name", "status", "call_id", "call_link_id", "duration_s",
                 "answered_at", "lead_score", "qualified", "claimed_by", "claimed_at")
    for ct in blob:
        if not isinstance(ct, dict):
            continue
        phone = _canonical_phone(ct.get("phone")) or str(ct.get("phone") or "").strip()
        if not phone:
            continue
        result = {k: v for k, v in ct.items() if k not in _row_cols and k != "ended"}
        lead_score = ct.get("lead_score")
        try:
            lead_score = int(lead_score) if lead_score is not None else None
        except (TypeError, ValueError):
            lead_score = None
        res = await db.execute(
            text(
                "INSERT INTO outbound_campaign_contacts "
                "(id, campaign_id, phone, name, call_link_id, status, attempt, call_id, "
                " answered_at, duration_s, lead_score, qualified, claimed_by, claimed_at, result, snapshot) "
                "VALUES (:id, :cid, :phone, :name, :clid, :status, :attempt, :call_id, "
                " CAST(:answered_at AS timestamptz), :duration_s, :lead_score, :qualified, "
                " CAST(:claimed_by AS uuid), CAST(:claimed_at AS timestamptz), "
                " CAST(:result AS jsonb), '{}'::jsonb) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": uuid.uuid4(), "cid": str(campaign.id), "phone": phone,
                "name": ct.get("name") or None,
                "clid": str(ct.get("call_link_id") or uuid.uuid4()),
                "status": _blob_row_status(ct),
                "attempt": 1 if ct.get("call_id") else 0,
                "call_id": ct.get("call_id"),
                "answered_at": ct.get("answered_at") or None,
                "duration_s": ct.get("duration_s"),
                "lead_score": lead_score,
                "qualified": bool(ct.get("qualified")) or ct.get("interest_outcome") == "interested",
                "claimed_by": str(ct["claimed_by"]) if ct.get("claimed_by") else None,
                "claimed_at": ct.get("claimed_at") or None,
                "result": json.dumps(result, default=str),
            },
        )
        inserted += int(res.rowcount or 0)
    campaign.contacts = None
    total = (await db.execute(
        text("SELECT count(*) FROM outbound_campaign_contacts WHERE campaign_id = :c"),
        {"c": str(campaign.id)},
    )).scalar_one()
    campaign.total_count = int(total)
    db.add(campaign)
    await db.commit()
    if blob:
        logger.info(
            "NOKVO-MIGRATE-V2: campaign %s blob → rows (%d in blob, %d inserted, %d total)",
            campaign.id, len(blob), inserted, total,
        )
    return inserted


async def rearm_unreached(db, campaign_id: uuid.UUID, buckets: tuple[str, ...] = ("no_pickup",)) -> int:
    """Re-arm terminal rows for a re-run, per bucket:

    * ``no_pickup`` — ``no_answer``/``failed`` → ``pending`` with a FRESH
      ``call_link_id`` (so a stale webhook from the previous attempt can never
      touch the retry) and the old placement id cleared. ``dnd_dropped`` stays
      dropped (still on the register).
    * ``busy`` — connected contacts who ASKED TO BE CALLED BACK
      (``result.callback_requested``, not qualified) → ``pending``, with the
      previous call's verdict fields cleared so the fresh call re-stamps them
      (and the flag removed so the row leaves the busy bucket immediately).

    Qualified and plain not-interested rows are never re-dialed. No DND scrub
    here — the dialer re-scrubs each claimed batch at dial time. Returns rows
    re-armed across the requested buckets."""
    rearmed = 0
    if "no_pickup" in buckets:
        res = await db.execute(
            text(
                "UPDATE outbound_campaign_contacts SET status = 'pending', "
                "call_link_id = CAST(gen_random_uuid() AS text), call_id = NULL, "
                "updated_at = now() "
                "WHERE campaign_id = :c AND status IN ('no_answer', 'failed')"
            ),
            {"c": str(campaign_id)},
        )
        rearmed += int(res.rowcount or 0)
    if "busy" in buckets:
        res = await db.execute(
            text(
                "UPDATE outbound_campaign_contacts SET status = 'pending', "
                "call_link_id = CAST(gen_random_uuid() AS text), call_id = NULL, "
                "answered_at = NULL, duration_s = NULL, lead_score = NULL, "
                "result = result - 'callback_requested', updated_at = now() "
                f"WHERE campaign_id = :c AND {BUCKET_SQL['busy']}"
            ),
            {"c": str(campaign_id)},
        )
        rearmed += int(res.rowcount or 0)
    await db.commit()
    return rearmed


async def requeue_abandoned(db, call_link_id: str) -> uuid.UUID | None:
    """The callee picked up but no conversation slot was free — put the row
    straight back to ``pending`` for a fresh dial.

    This is the safety net behind the conversation cap, not a normal path: with
    ``ring_multiplier`` at 1.0 it should never fire at all, and the pacer treats
    every occurrence as abandon-rate signal to back off. It exists because the
    alternative — what the code did before — was to let the person say "hello"
    into a socket we then closed, and file them as a missed call.

    ``attempt`` is deliberately NOT rolled back and no retry is scheduled: this
    was our failure, not an unreachable contact, so it must not consume one of
    the contact's attempts. A fresh ``call_link_id`` keeps any late webhook from
    the abandoned placement off the requeued row (same reasoning as
    :func:`rearm_unreached`). ``result.abandoned_at`` is the audit trail and the
    pacer's input. Guarded on the pre-answer statuses so a racing hangup webhook
    can't be undone. Returns the campaign_id when this call requeued the row."""
    row = (await db.execute(
        text(
            "UPDATE outbound_campaign_contacts "
            "SET status = 'pending', call_link_id = CAST(gen_random_uuid() AS text), "
            "    call_id = NULL, answered_at = NULL, abandoned = true, "
            "    result = COALESCE(result, '{}'::jsonb) "
            "             || jsonb_build_object('abandoned_at', to_jsonb(now())), "
            "    updated_at = now() "
            "WHERE call_link_id = :clid AND status IN ('dialing', 'ringing') "
            "RETURNING campaign_id"
        ),
        {"clid": call_link_id},
    )).first()
    return row[0] if row else None


async def pending_count(db, campaign_id: uuid.UUID) -> int:
    """Rows still waiting to dial (used by re-run to decide if there's work)."""
    return int((await db.execute(
        text("SELECT count(*) FROM outbound_campaign_contacts "
             "WHERE campaign_id = :c AND status = 'pending'"),
        {"c": str(campaign_id)},
    )).scalar_one())


# Stuck-row reaper timeouts. A row leaves a "live" status only when a Plivo
# webhook lands; a lost webhook (deploy mid-call, process restart between claim
# and placement, callback retries exhausted) would otherwise pin the row live
# FOREVER — permanently eating one of the 5 dial slots and, once the rest of
# the list drains, blocking the running→completed transition (the campaign
# would hold the tenant's one-campaign slot with nothing left to dial).
_STALE_PLACING_MINUTES = 30    # dialing/ringing — a real ring resolves in ~1 min
_STALE_ANSWERED_MINUTES = 240  # answered — no legitimate call runs 4 hours


async def reap_stale_rows(db) -> list[uuid.UUID]:
    """Sweep rows stuck in a live status on RUNNING campaigns past the timeouts:
    ``dialing``/``ringing`` → ``no_answer`` (never actually reached — a re-run
    re-arms them), ``answered`` → ``completed`` (the person WAS reached; only the
    hangup webhook was lost — never re-dial them). Returns the distinct campaign
    ids touched so the caller can run :func:`maybe_complete` on each. Called from
    the campaign ticker every 10 minutes; idempotent and cheap (indexed status
    scan, no-op when nothing is stale)."""
    placing = (await db.execute(
        text(
            "UPDATE outbound_campaign_contacts occ SET status = 'no_answer', updated_at = now() "
            "FROM outbound_campaigns c WHERE c.id = occ.campaign_id AND c.status = 'running' "
            "AND occ.status IN ('dialing', 'ringing') "
            "AND occ.updated_at < now() - make_interval(mins => :m) "
            "RETURNING occ.campaign_id"
        ),
        {"m": _STALE_PLACING_MINUTES},
    )).all()
    answered = (await db.execute(
        text(
            "UPDATE outbound_campaign_contacts occ SET status = 'completed', updated_at = now() "
            "FROM outbound_campaigns c WHERE c.id = occ.campaign_id AND c.status = 'running' "
            "AND occ.status = 'answered' "
            "AND occ.updated_at < now() - make_interval(mins => :m) "
            "RETURNING occ.campaign_id"
        ),
        {"m": _STALE_ANSWERED_MINUTES},
    )).all()
    await db.commit()
    touched = {r[0] for r in placing} | {r[0] for r in answered}
    if touched:
        logger.warning(
            "NOKVO-REAPER: released %d stale contact row(s) across %d campaign(s)",
            len(placing) + len(answered), len(touched),
        )
    return list(touched)


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


async def summary(db, campaign_id: uuid.UUID) -> dict[str, Any]:
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


async def _summary_uncached(db, campaign_id: uuid.UUID) -> dict[str, Any]:
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
    # Connected-but-asked-to-call-back (the "busy" bucket) — carved OUT of
    # not_interested so the three connected outcomes stay mutually exclusive.
    busy = (await db.execute(
        text(f"SELECT count(*) FROM outbound_campaign_contacts "
             f"WHERE campaign_id = :c AND {BUCKET_SQL['busy']}"),
        {"c": str(campaign_id)},
    )).scalar_one()
    # Retries scheduled but not yet due. Surfaced so a completed campaign that
    # will start dialing again tomorrow reads as "23 retries scheduled, next
    # 4:10 PM" rather than a finished campaign inexplicably coming back to life.
    retry_row = (await db.execute(
        text("SELECT count(*), min(next_attempt_at) FROM outbound_campaign_contacts "
             "WHERE campaign_id = :c AND status = 'no_answer' AND next_attempt_at IS NOT NULL"),
        {"c": str(campaign_id)},
    )).first()
    retry_scheduled = int(retry_row[0] or 0)
    next_retry_at = retry_row[1].isoformat() if retry_row and retry_row[1] else None

    total = sum(by_status.values())
    return {
        "total": total,
        "retry_scheduled": retry_scheduled,
        "next_retry_at": next_retry_at,
        "qualified": int(qualified),
        "not_interested": max(0, by_status.get("completed", 0) - int(qualified) - int(busy)),
        "busy": int(busy),
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
        # call_link_id is the transcript/ledger join key (the internal call id);
        # call_id is Plivo's placement uuid — the Call Logs tab needs both.
        text(f"SELECT id, phone, name, status, lead_score, qualified, result, "
             f"call_id, call_link_id, answered_at, duration_s "
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
    from app.services.agent_outbound_context import questionnaire_max_points

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
        # Weighted/graded points via the ONE shared helper, so the denominator
        # never drifts from the scorer (len(questions) broke on weighted tiers).
        "max_score": cfg.get("max_score")
        or (questionnaire_max_points(q.get("questions")) if has_q else None),
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
