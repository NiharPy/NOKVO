"""Durable call transcripts — store at teardown, render for download, purge on a
rolling 1-month retention.

- :meth:`store` upserts one row per call (best-effort; never raises out — teardown
  must not break on a transcript failure).
- :meth:`render_txt` builds the per-call ``.txt`` download.
- :meth:`purge_expired` deletes transcripts older than the rolling window; it runs
  both lazily (on the list endpoint, scoped to the org) and via :meth:`run_forever`
  (a daily all-orgs sweep started at app startup).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.call_transcript import CallTranscript

logger = logging.getLogger(__name__)

# Rolling retention window. "1 month" ≈ 30 days (swap to dateutil relativedelta
# for a calendar month if ever needed — a one-line change).
RETENTION = timedelta(days=30)
_PURGE_INTERVAL_SECONDS = 24 * 60 * 60

_ROLE_LABELS = {"assistant": "Agent", "agent": "Agent", "user": "Caller", "system": "System"}


def _normalize_turns(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Map raw session history to compact ``{role, content}`` turns, dropping
    empty content and collapsing whitespace."""
    turns: list[dict[str, str]] = []
    for item in history or []:
        role = str((item or {}).get("role") or "").strip() or "turn"
        content = " ".join(str((item or {}).get("content") or "").split())
        if content:
            turns.append({"role": role, "content": content})
    return turns


class TranscriptService:
    @staticmethod
    async def store(
        db,
        *,
        organization_id: Any,
        tenant_id: str,
        call_id: str,
        history: list[dict[str, Any]] | None,
        duration_seconds: float | int = 0,
        kind: str = "inbound",
        caller_phone: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> bool:
        """Upsert the call's transcript (idempotent on ``call_id`` — a retry
        overwrites the turns). Best-effort: returns ``False`` and never raises on
        any error. No-op when there are no usable turns."""
        turns = _normalize_turns(history)
        if not turns or not call_id:
            return False
        now = datetime.now(timezone.utc)
        ended = ended_at or now
        started = started_at or (ended - timedelta(seconds=float(duration_seconds or 0)))
        values = {
            "organization_id": organization_id,
            "tenant_id": tenant_id,
            "call_id": call_id,
            "kind": kind or "inbound",
            "caller_phone": (caller_phone or None),
            "turns": turns,
            "turn_count": len(turns),
            "duration_seconds": duration_seconds or 0,
            "started_at": started,
            "ended_at": ended,
        }
        try:
            stmt = pg_insert(CallTranscript).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["call_id"],
                set_={
                    "turns": stmt.excluded.turns,
                    "turn_count": stmt.excluded.turn_count,
                    "duration_seconds": stmt.excluded.duration_seconds,
                    "ended_at": stmt.excluded.ended_at,
                    "caller_phone": stmt.excluded.caller_phone,
                },
            )
            await db.execute(stmt)
            await db.commit()
            return True
        except Exception:
            logger.debug("NOKVO-TRANSCRIPT: store failed for call %s", call_id, exc_info=True)
            try:
                await db.rollback()
            except Exception:
                pass
            return False

    @staticmethod
    def render_txt(
        *,
        turns: list[dict[str, Any]] | None,
        started_at: Any = None,
        kind: str | None = None,
        duration_seconds: Any = None,
        caller_phone: str | None = None,
    ) -> str:
        """A readable ``.txt`` transcript: a small header + ``Agent:``/``Caller:``
        lines, one per turn."""
        header = ["NOKVO call transcript"]
        if started_at:
            header.append(f"Date: {started_at}")
        if kind:
            header.append(f"Type: {kind}")
        if caller_phone:
            header.append(f"Caller: {caller_phone}")
        if duration_seconds is not None:
            try:
                header.append(f"Duration: {int(float(duration_seconds))}s")
            except (TypeError, ValueError):
                pass
        out = ["\n".join(header), "-" * 40]
        for t in turns or []:
            role = str((t or {}).get("role") or "turn")
            label = _ROLE_LABELS.get(role.lower(), role.title())
            content = str((t or {}).get("content") or "").strip()
            if content:
                out.append(f"{label}: {content}")
        return "\n".join(out) + "\n"

    @staticmethod
    async def purge_expired(db, organization_id: Any = None, *, now: datetime | None = None) -> int:
        """Delete transcripts older than the rolling retention window. Scoped to
        one org when ``organization_id`` is given, else all orgs. Returns the
        number of rows deleted."""
        cutoff = (now or datetime.now(timezone.utc)) - RETENTION
        stmt = delete(CallTranscript).where(CallTranscript.started_at < cutoff)
        if organization_id is not None:
            stmt = stmt.where(CallTranscript.organization_id == organization_id)
        res = await db.execute(stmt)
        await db.commit()
        return int(getattr(res, "rowcount", 0) or 0)

    @classmethod
    async def run_forever(cls) -> None:
        """Daily all-orgs retention sweep. Started as a background task at app
        startup; never raises out of the loop. Leader-elected so only one
        replica runs the sweep per window — the delete is idempotent, but this
        stops every replica re-running the same DELETE daily."""
        from app.db.session import AsyncSessionLocal
        from app.services.scheduler_leader import scheduler_leader

        while True:
            try:
                # Hold the lock for ~the whole window so a replica that wakes on
                # an offset timer doesn't re-purge the same day; it expires
                # before the next cycle so leadership can move if a pod cycles.
                async with scheduler_leader(
                    "transcript-retention", ttl_seconds=_PURGE_INTERVAL_SECONDS - 3600
                ) as is_leader:
                    if is_leader:
                        async with AsyncSessionLocal() as db:
                            deleted = await cls.purge_expired(db)
                        if deleted:
                            logger.info("NOKVO-TRANSCRIPT: retention purged %d expired transcript(s)", deleted)
            except Exception:
                logger.warning("NOKVO-TRANSCRIPT: retention sweep failed", exc_info=True)
            await asyncio.sleep(_PURGE_INTERVAL_SECONDS)
