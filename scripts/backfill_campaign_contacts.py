"""One-off backfill: migrate legacy blob campaigns to per-row storage (V2).

For every ``outbound_campaigns`` row that still carries a ``contacts`` JSONB blob,
insert the equivalent ``outbound_campaign_contacts`` rows (hot fields → columns,
cold detail → ``result``), then NULL the blob so the campaign reads as V2. Safe
to re-run: ON CONFLICT (campaign_id, phone) DO NOTHING, and campaigns already
migrated (contacts IS NULL) are skipped.

Run from repo root AFTER ``alembic upgrade head``, with the flag about to be
flipped:
    source venv/bin/activate
    python3 scripts/backfill_campaign_contacts.py            # apply
    python3 scripts/backfill_campaign_contacts.py --dry-run  # report only
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime

import asyncpg

from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("occ-backfill")

_RESULT_KEYS = (
    "score_breakdown", "call_note", "call_note_generated_at", "lead_score_reason",
    "interest_reason", "interest_outcome", "max_score", "claimed_by_name",
    "claim_status", "from_number", "error",
)


def _row_status(ct: dict) -> str:
    st = str(ct.get("status") or "").lower()
    scored = ct.get("qualified") is not None or ct.get("lead_score") is not None or ct.get("interest_outcome")
    if scored or (ct.get("answered_at") and st in ("answered", "calling", "")):
        return "completed"
    if st in ("failed",):
        return "failed"
    if st in ("pending", "dialing", "ringing"):
        return st if st != "dialing" and st != "ringing" else "pending"
    if ct.get("ended") and not ct.get("answered_at"):
        return "no_answer"
    return st or "pending"


def _dt(v):
    """Parse an ISO-8601 string (blob stores timestamps as strings) to a datetime,
    or pass through an existing datetime; None on anything unparseable."""
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _to_row(campaign_id, ct: dict) -> tuple:
    result = {k: ct[k] for k in _RESULT_KEYS if ct.get(k) is not None}
    claimed_by = ct.get("claimed_by")
    try:
        claimed_by = uuid.UUID(str(claimed_by)) if claimed_by else None
    except (ValueError, TypeError):
        claimed_by = None
    dur = ct.get("duration_s")
    try:
        dur = float(dur) if dur is not None else None
    except (ValueError, TypeError):
        dur = None
    return (
        uuid.uuid4(), campaign_id,
        str(ct.get("phone") or ""), ct.get("name"),
        _row_status(ct), ct.get("call_id"),
        str(ct.get("call_link_id") or uuid.uuid4()),
        bool(ct.get("qualified")) if ct.get("qualified") is not None else False,
        int(ct["lead_score"]) if isinstance(ct.get("lead_score"), (int, float)) else None,
        _dt(ct.get("answered_at")), dur, claimed_by, _dt(ct.get("claimed_at")),
        json.dumps(result),
    )


async def main() -> int:
    dry = "--dry-run" in sys.argv[1:]
    c = await asyncpg.connect(
        host=settings.POSTGRES_SERVER, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, database=settings.POSTGRES_DB, ssl="require",
    )
    migrated = rows_ins = 0
    try:
        camps = await c.fetch(
            "SELECT id, contacts FROM outbound_campaigns "
            "WHERE contacts IS NOT NULL AND jsonb_array_length(contacts) > 0"
        )
        log.info("found %d blob campaign(s) to migrate%s", len(camps), " [DRY RUN]" if dry else "")
        for camp in camps:
            cid = camp["id"]
            blob = camp["contacts"]
            contacts = json.loads(blob) if isinstance(blob, str) else blob
            recs = [_to_row(cid, ct) for ct in contacts if ct.get("phone")]
            if dry:
                log.info("  campaign %s: %d contacts (dry-run)", cid, len(recs))
                migrated += 1
                rows_ins += len(recs)
                continue
            async with c.transaction():
                await c.execute(
                    "CREATE TEMP TABLE _bf (id uuid, campaign_id uuid, phone text, name text, "
                    "status text, call_id text, call_link_id text, qualified boolean, lead_score int, "
                    "answered_at timestamptz, duration_s numeric, claimed_by uuid, claimed_at timestamptz, "
                    "result jsonb) ON COMMIT DROP"
                )
                await c.copy_records_to_table(
                    "_bf", records=recs,
                    columns=["id", "campaign_id", "phone", "name", "status", "call_id", "call_link_id",
                             "qualified", "lead_score", "answered_at", "duration_s", "claimed_by",
                             "claimed_at", "result"],
                )
                res = await c.execute(
                    "INSERT INTO outbound_campaign_contacts "
                    "(id, campaign_id, phone, name, status, call_id, call_link_id, qualified, lead_score, "
                    " answered_at, duration_s, claimed_by, claimed_at, result, attempt, snapshot) "
                    "SELECT id, campaign_id, phone, name, status, call_id, call_link_id, qualified, lead_score, "
                    " answered_at, duration_s, claimed_by, claimed_at, result, 0, '{}'::jsonb FROM _bf "
                    "ON CONFLICT (campaign_id, phone) DO NOTHING"
                )
                # Retire the blob → campaign now reads as V2.
                await c.execute("UPDATE outbound_campaigns SET contacts = NULL WHERE id = $1", cid)
            try:
                rows_ins += int(res.split()[-1])
            except (ValueError, IndexError):
                pass
            migrated += 1
            log.info("  campaign %s: migrated %d contacts", cid, len(recs))
    finally:
        await c.close()
    log.info("done: campaigns=%d rows_inserted=%d%s", migrated, rows_ins, " [DRY RUN]" if dry else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
