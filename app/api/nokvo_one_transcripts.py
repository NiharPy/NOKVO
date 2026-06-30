"""Transcripts page API — list calls + view/download their transcripts.

The authoritative call list is the billing ledger (``CallCost`` — one row per
call, never purged); each row is annotated with whether its transcript is still
retained (``CallTranscript``, rolling 1-month retention). View + per-call ``.txt``
download read ``CallTranscript``. Org-scoped to the caller's organization.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.models.call_cost import CallCost
from app.models.call_transcript import CallTranscript
from app.models.organization_user import OrganizationUser
from app.services.transcript_service import TranscriptService

router = APIRouter()


def _org_user():
    # Accept BOTH Nokvo One and NOKVO APEX orgs — transcripts are org-scoped
    # (_load filters by organization_id), so each product only reads its own.
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["active"],
        allowed_product_tiers=["nokvo_one", "nokvo_apex"],
    )


@router.get("/transcripts")
async def list_transcripts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: OrganizationUser = Depends(_org_user()),
    db: AsyncSession = Depends(deps.get_db),
):
    """All calls (newest first) with a ``has_transcript`` flag. Lazily purges this
    org's expired transcripts first (backstop to the daily sweep)."""
    org_id = user.organization_id
    try:
        await TranscriptService.purge_expired(db, org_id)
    except Exception:
        pass

    rows = (
        await db.execute(
            select(
                CallCost.call_id,
                CallCost.kind,
                CallCost.duration_seconds,
                CallCost.started_at,
                CallCost.ended_at,
            )
            .where(CallCost.organization_id == org_id)
            .order_by(CallCost.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    call_ids = [r.call_id for r in rows]
    have: set[str] = set()
    if call_ids:
        ht = await db.execute(
            select(CallTranscript.call_id).where(
                CallTranscript.organization_id == org_id,
                CallTranscript.call_id.in_(call_ids),
            )
        )
        have = {c for (c,) in ht.all()}

    items = [
        {
            "call_id": r.call_id,
            "kind": r.kind,
            "duration_seconds": str(r.duration_seconds) if r.duration_seconds is not None else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "has_transcript": r.call_id in have,
        }
        for r in rows
    ]
    return {"transcripts": items, "limit": limit, "offset": offset, "count": len(items)}


async def _load(db: AsyncSession, org_id, call_id: str) -> CallTranscript:
    res = await db.execute(
        select(CallTranscript).where(
            CallTranscript.organization_id == org_id,
            CallTranscript.call_id == call_id,
        )
    )
    row = res.scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Transcript not found or expired")
    return row


@router.get("/transcripts/{call_id}")
async def get_transcript(
    call_id: str,
    user: OrganizationUser = Depends(_org_user()),
    db: AsyncSession = Depends(deps.get_db),
):
    row = await _load(db, user.organization_id, call_id)
    return {
        "call_id": row.call_id,
        "kind": row.kind,
        "caller_phone": row.caller_phone,
        "duration_seconds": str(row.duration_seconds) if row.duration_seconds is not None else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "turn_count": row.turn_count,
        "turns": row.turns or [],
    }


@router.get("/transcripts/{call_id}/download")
async def download_transcript(
    call_id: str,
    user: OrganizationUser = Depends(_org_user()),
    db: AsyncSession = Depends(deps.get_db),
):
    row = await _load(db, user.organization_id, call_id)
    text = TranscriptService.render_txt(
        turns=row.turns or [],
        started_at=row.started_at.isoformat() if row.started_at else None,
        kind=row.kind,
        duration_seconds=row.duration_seconds,
        caller_phone=row.caller_phone,
    )
    safe = "".join(c for c in call_id if c.isalnum() or c in "-_") or "transcript"
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="transcript_{safe}.txt"'},
    )
