"""Answer-rate-aware ring-ahead for APEX campaigns.

The conversation cap (an org's plan concurrency) is a hard promise: never more
simultaneous conversations than the customer bought. Dialing exactly that many
lines honours it but wastes most of the capacity, because most calls are never
answered — one line ringing for a 25s timeout to produce a 25%-likely
conversation leaves the single slot idle three quarters of the time.

Predictive dialers solve this by ringing MORE lines than they have agents and
accepting a small abandon rate. We do the same with one difference that matters:
an abandon here is a person who said "hello" to a machine that hung up, so the
ceiling is enforced from measured behaviour, not assumed, and the pacer collapses
back to safe 1:1 the instant it is breached.

Two numbers, both measured per campaign from rows the dialer already writes:

* **answer rate** — connects over completed dial attempts. The multiplier is its
  reciprocal, so ``free_slots × multiplier`` lines ringing produces, in
  expectation, exactly ``free_slots`` answers.
* **abandon rate** — how often a connect found no free slot
  (``result.abandoned_at``, stamped by ``campaign_contacts_v2.requeue_abandoned``).
  Over ``APEX_PACER_MAX_ABANDON_PCT`` the multiplier drops to 1.0 until it
  recovers.

Both are Redis-cached on the same short TTL as the dashboard summary — the
dialer reads them on every refill and they move slowly. Every failure path
returns the safe 1:1, so a Redis outage or a malformed row can only ever make the
pacer more conservative.
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import text

from app.core.config import settings

logger = logging.getLogger(__name__)

# Matches campaign_contacts_v2._SUMMARY_TTL_S: these are dashboard-grade numbers
# read on a hot path, and a few seconds of staleness cannot move the multiplier
# far enough to matter.
_PACER_TTL_S = 5

_SAFE = (1.0, None)  # one line per free slot, no ceiling needed — zero abandons

# Rows that represent a finished dial attempt, and the subset that connected.
# 'completed' is an answered call that has since hung up; 'answered' is live.
_CONNECTED = ("answered", "completed")
_ATTEMPTED = ("answered", "completed", "no_answer", "failed")


async def _stats(db, campaign_id: uuid.UUID) -> dict[str, int]:
    """Connects, attempts and abandons for a campaign, in one indexed pass."""
    row = (await db.execute(
        text(
            "SELECT "
            "  count(*) FILTER (WHERE status = ANY(:connected)) AS connected, "
            "  count(*) FILTER (WHERE status = ANY(:attempted)) AS attempted, "
            "  count(*) FILTER (WHERE result ? 'abandoned_at')  AS abandoned "
            "FROM outbound_campaign_contacts WHERE campaign_id = :c"
        ),
        {"c": str(campaign_id), "connected": list(_CONNECTED), "attempted": list(_ATTEMPTED)},
    )).first()
    return {
        "connected": int(row[0] or 0),
        "attempted": int(row[1] or 0),
        "abandoned": int(row[2] or 0),
    }


async def _cached_stats(db, campaign_id: uuid.UUID) -> dict[str, int]:
    key = f"occ:pacer:{campaign_id}"
    try:
        from app.services.agent_session_store import AgentSessionStore

        cached = await AgentSessionStore.client().get(key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    out = await _stats(db, campaign_id)
    try:
        from app.services.agent_session_store import AgentSessionStore

        await AgentSessionStore.client().setex(key, _PACER_TTL_S, json.dumps(out))
    except Exception:
        pass
    return out


async def ring_multiplier_for(db, campaign_id: uuid.UUID) -> tuple[float, int | None]:
    """``(ring_multiplier, max_ring_ahead)`` for this campaign's next claim.

    Returns the safe ``(1.0, None)`` — one line per free conversation slot —
    whenever the pacer is off, the sample is too small to trust, or the campaign
    is already abandoning calls. Never raises."""
    if not settings.APEX_PACER_ENABLED:
        return _SAFE
    try:
        stats = await _cached_stats(db, campaign_id)
        attempted = stats["attempted"]
        # Too few finished attempts to estimate anything. Guessing here is how a
        # cold campaign would blow through its first hundred contacts abandoning
        # every connect, so we simply don't pace until the campaign has spoken.
        if attempted < max(1, int(settings.APEX_PACER_MIN_SAMPLE)):
            return _SAFE

        connected = stats["connected"]
        if connected > 0:
            abandon_pct = 100.0 * stats["abandoned"] / connected
            if abandon_pct > float(settings.APEX_PACER_MAX_ABANDON_PCT):
                logger.warning(
                    "NOKVO-PACER: campaign=%s abandon %.1f%% over ceiling %.1f%% — back to 1:1",
                    campaign_id, abandon_pct, float(settings.APEX_PACER_MAX_ABANDON_PCT),
                )
                return _SAFE

        answer_rate = connected / attempted
        # Floor the rate, not the multiplier: a campaign hitting a dead list would
        # otherwise divide by something near zero and ask for hundreds of lines.
        answer_rate = max(float(settings.APEX_PACER_MIN_ANSWER_RATE), min(1.0, answer_rate))
        multiplier = 1.0 / answer_rate
        ceiling = max(1, int(settings.APEX_PACER_MAX_RING_AHEAD))
        logger.info(
            "NOKVO-PACER: campaign=%s answer_rate=%.2f multiplier=%.2f ceiling=%d "
            "(connected=%d attempted=%d abandoned=%d)",
            campaign_id, answer_rate, multiplier, ceiling,
            connected, attempted, stats["abandoned"],
        )
        return multiplier, ceiling
    except Exception:
        logger.exception("NOKVO-PACER: falling back to 1:1 for campaign=%s", campaign_id)
        return _SAFE


async def invalidate(campaign_id: uuid.UUID) -> None:
    """Drop the cached stats — called when a campaign re-arms a batch, so the
    pacer re-reads instead of pacing a re-run off the previous pass's numbers."""
    try:
        from app.services.agent_session_store import AgentSessionStore

        await AgentSessionStore.client().delete(f"occ:pacer:{campaign_id}")
    except Exception:
        pass
