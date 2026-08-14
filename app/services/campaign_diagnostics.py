"""Why a campaign performed the way it did.

The campaign summary answers "how many qualified". It cannot answer any of the
questions that actually change what an operator should do next: is this list full
of dead numbers or busy people, is 4pm better than 11am, which question is where
callers hang up, is the humanization rollout helping. Every one of those was
already implicit in rows the dialer writes — nothing here needs new instrumentation
on the call path, only the columns added in ``apex_call_diagnostics_v1``.

Deliberately server-side aggregation: these run over a campaign that may hold a
million rows, so every figure is one indexed GROUP BY, never a row scan in Python.

The ``retry_readiness`` block is the input to the retry cadence policy. Retry
offsets cannot be tuned blind — a list that is mostly disconnected numbers wants
no retry at all, while a list of busy people wants a different hour — so the
cause histogram and the observed repeat-dial lift are reported before the policy
is set.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Hangup causes that mean "this number will never work", as opposed to "nobody
# picked up this time". Advisory only — the endpoint reports the observed
# histogram so the real denylist is set from data rather than from this guess.
_TERMINAL_CAUSE_HINTS = (
    "INVALID", "UNALLOCATED", "DOES_NOT_EXIST", "REJECTED", "BLOCKED", "BARRED",
)


async def _by_hour(db, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    """Answer rate by hour of day (IST) — the single cheapest lever on connect
    rate, and currently invisible."""
    rows = (await db.execute(
        text(
            "SELECT EXTRACT(HOUR FROM (updated_at AT TIME ZONE 'Asia/Kolkata'))::int AS hr, "
            "  count(*) AS dialed, "
            "  count(*) FILTER (WHERE status IN ('answered', 'completed')) AS answered "
            "FROM outbound_campaign_contacts "
            "WHERE campaign_id = :c AND call_id IS NOT NULL "
            "GROUP BY hr ORDER BY hr"
        ),
        {"c": str(campaign_id)},
    )).all()
    return [
        {
            "hour": int(r[0]),
            "dialed": int(r[1]),
            "answered": int(r[2]),
            "answer_rate": round(int(r[2]) / int(r[1]), 4) if r[1] else 0.0,
        }
        for r in rows
    ]


async def _by_weekday(db, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (await db.execute(
        text(
            "SELECT EXTRACT(ISODOW FROM (updated_at AT TIME ZONE 'Asia/Kolkata'))::int AS dow, "
            "  count(*) AS dialed, "
            "  count(*) FILTER (WHERE status IN ('answered', 'completed')) AS answered "
            "FROM outbound_campaign_contacts "
            "WHERE campaign_id = :c AND call_id IS NOT NULL "
            "GROUP BY dow ORDER BY dow"
        ),
        {"c": str(campaign_id)},
    )).all()
    names = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
    return [
        {
            "weekday": names.get(int(r[0]), str(r[0])),
            "dialed": int(r[1]),
            "answered": int(r[2]),
            "answer_rate": round(int(r[2]) / int(r[1]), 4) if r[1] else 0.0,
        }
        for r in rows
    ]


async def _hangup_causes(db, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    """The histogram that turns one 'no_pickup' bucket back into the four
    different problems it was hiding."""
    rows = (await db.execute(
        text(
            "SELECT COALESCE(NULLIF(hangup_cause, ''), 'UNKNOWN') AS cause, count(*) AS n "
            "FROM outbound_campaign_contacts "
            "WHERE campaign_id = :c AND status IN ('no_answer', 'failed') "
            "GROUP BY cause ORDER BY n DESC"
        ),
        {"c": str(campaign_id)},
    )).all()
    return [
        {
            "cause": str(r[0]),
            "count": int(r[1]),
            "likely_permanent": any(h in str(r[0]).upper() for h in _TERMINAL_CAUSE_HINTS),
        }
        for r in rows
    ]


async def _talk_time(db, campaign_id: uuid.UUID) -> dict[str, Any]:
    """Duration distribution for connected calls, with the count that matters
    most on outbound: how many hung up inside the first ten seconds. A high
    early-hangup share is an opener problem, not a list problem."""
    row = (await db.execute(
        text(
            "SELECT count(*) AS connected, "
            "  count(*) FILTER (WHERE duration_s < 10)  AS under_10s, "
            "  count(*) FILTER (WHERE duration_s < 30)  AS under_30s, "
            "  ROUND(AVG(duration_s), 1)                AS avg_s, "
            "  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration_s) AS median_s "
            "FROM outbound_campaign_contacts "
            "WHERE campaign_id = :c AND duration_s IS NOT NULL"
        ),
        {"c": str(campaign_id)},
    )).first()
    connected = int(row[0] or 0)
    return {
        "connected": connected,
        "hung_up_under_10s": int(row[1] or 0),
        "hung_up_under_30s": int(row[2] or 0),
        "early_hangup_rate": round(int(row[1] or 0) / connected, 4) if connected else 0.0,
        "avg_seconds": float(row[3]) if row[3] is not None else None,
        "median_seconds": float(row[4]) if row[4] is not None else None,
    }


async def _by_variant(db, campaign_id: uuid.UUID) -> dict[str, Any]:
    """Outcomes split by which opener/TTS rendition the call spoke.

    This is the whole point of recording the variants: the humanization knobs
    stop being an untested opinion the moment their outcomes can be compared.
    """
    async def _split(column: str) -> list[dict[str, Any]]:
        rows = (await db.execute(
            text(
                f"SELECT {column} AS v, count(*) AS connected, "
                "  count(*) FILTER (WHERE qualified) AS qualified, "
                "  ROUND(AVG(duration_s), 1) AS avg_s "
                "FROM outbound_campaign_contacts "
                f"WHERE campaign_id = :c AND {column} IS NOT NULL "
                "  AND status IN ('answered', 'completed') "
                f"GROUP BY {column} ORDER BY {column}"
            ),
            {"c": str(campaign_id)},
        )).all()
        return [
            {
                "variant": int(r[0]),
                "connected": int(r[1]),
                "qualified": int(r[2]),
                "qualify_rate": round(int(r[2]) / int(r[1]), 4) if r[1] else 0.0,
                "avg_seconds": float(r[3]) if r[3] is not None else None,
            }
            for r in rows
        ]

    return {"opener": await _split("opener_variant"), "tts": await _split("tts_variant")}


async def _question_dropoff(db, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    """How far through the questionnaire callers get before the call ends.

    Read from the per-call ``questionnaire_progress.delivered`` set the verbatim
    path already persists — the authoritative record of which questions were
    actually ASKED, which is exactly what a drop-off curve needs. The question
    that loses the most callers is the one an operator should rewrite, and until
    now nothing in the product could name it.
    """
    rows = (await db.execute(
        text(
            "SELECT COALESCE(jsonb_array_length(result->'questions_delivered'), 0) AS reached, "
            "  count(*) AS n "
            "FROM outbound_campaign_contacts "
            "WHERE campaign_id = :c AND status IN ('answered', 'completed') "
            "GROUP BY reached ORDER BY reached"
        ),
        {"c": str(campaign_id)},
    )).all()
    total = sum(int(r[1]) for r in rows) or 0
    out: list[dict[str, Any]] = []
    remaining = total
    for r in rows:
        reached, n = int(r[0]), int(r[1])
        out.append({
            "questions_reached": reached,
            "calls": n,
            "share": round(n / total, 4) if total else 0.0,
            "still_engaged": remaining,
        })
        remaining -= n
    return out


async def _retry_readiness(db, campaign_id: uuid.UUID) -> dict[str, Any]:
    """Everything needed to choose retry offsets from evidence instead of taste.

    ``repeat_dial_lift`` compares the answer rate on first attempts with the rate
    on later ones. Those later attempts already exist — they are the manual
    Re-run traffic — so this estimates the value of an automatic cadence BEFORE
    one ships.
    """
    row = (await db.execute(
        text(
            "SELECT "
            "  count(*) FILTER (WHERE attempt <= 1) AS first_dialed, "
            "  count(*) FILTER (WHERE attempt <= 1 AND status IN ('answered','completed')) AS first_answered, "
            "  count(*) FILTER (WHERE attempt > 1) AS repeat_dialed, "
            "  count(*) FILTER (WHERE attempt > 1 AND status IN ('answered','completed')) AS repeat_answered "
            "FROM outbound_campaign_contacts "
            "WHERE campaign_id = :c AND call_id IS NOT NULL"
        ),
        {"c": str(campaign_id)},
    )).first()
    first_d, first_a = int(row[0] or 0), int(row[1] or 0)
    rep_d, rep_a = int(row[2] or 0), int(row[3] or 0)
    return {
        "first_attempt": {
            "dialed": first_d,
            "answered": first_a,
            "answer_rate": round(first_a / first_d, 4) if first_d else 0.0,
        },
        "repeat_attempts": {
            "dialed": rep_d,
            "answered": rep_a,
            "answer_rate": round(rep_a / rep_d, 4) if rep_d else 0.0,
        },
        # Enough repeat traffic to trust the comparison? Below this, set the
        # cadence conservatively and re-read after a week.
        "sample_sufficient": rep_d >= 50,
        "hangup_causes": await _hangup_causes(db, campaign_id),
    }


async def campaign_diagnostics(db, campaign_id: uuid.UUID) -> dict[str, Any]:
    """The full picture for one campaign. Each block is independently
    fail-softed: one slow or failing aggregate must not blank the whole page."""
    out: dict[str, Any] = {"campaign_id": str(campaign_id)}
    blocks = {
        "answer_rate_by_hour": _by_hour,
        "answer_rate_by_weekday": _by_weekday,
        "hangup_causes": _hangup_causes,
        "talk_time": _talk_time,
        "by_variant": _by_variant,
        "question_dropoff": _question_dropoff,
        "retry_readiness": _retry_readiness,
    }
    for name, fn in blocks.items():
        try:
            out[name] = await fn(db, campaign_id)
        except Exception:
            logger.exception("CAMPAIGN-DIAGNOSTICS: %s failed for %s", name, campaign_id)
            out[name] = None
    return out
