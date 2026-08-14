"""When — and whether — an unreached contact is worth dialing again.

A no-answer has always been terminal here: the only retry was an operator
noticing and pressing Re-run. First-attempt connect rates on Indian mobile lists
sit around 20-35%, so most of a list the customer paid for was never reached by
anyone. A three-attempt cadence spread across different hours and a different
weekday typically reaches 55-70% of the same list — the largest single lift
available in the product, and it was sitting behind a button nobody pressed.

The policy is deliberately thin and entirely config-driven, because the numbers
that matter cannot be chosen from first principles:

  * how many attempts are worth paying for depends on the list;
  * the best offsets depend on when that audience answers a phone;
  * which causes are dead ends depends on the carrier's vocabulary.

All three are answered by ``retry_readiness`` in the campaign diagnostics
endpoint. Until someone has read it, ``APEX_RETRY_ATTEMPTS`` stays at 1 and this
module schedules nothing at all — the mechanism ships wired but dark.

The one judgement encoded in code rather than config is the SHAPE of the offsets:
a retry at a different time of day, then on a different day. Re-dialing the same
number at the same hour tomorrow mostly re-tests the same unavailability.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.config import settings

logger = logging.getLogger(__name__)


def _offsets() -> list[float]:
    """Hours-after-failure for each successive retry, from config."""
    raw = str(getattr(settings, "APEX_RETRY_OFFSETS_HOURS", "") or "")
    out: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hours = float(part)
        except ValueError:
            logger.warning("APEX-RETRY: ignoring un-parseable offset %r", part)
            continue
        if hours > 0:
            out.append(hours)
    return out


def _skip_causes() -> tuple[str, ...]:
    raw = str(getattr(settings, "APEX_RETRY_SKIP_CAUSES", "") or "")
    return tuple(p.strip().upper() for p in raw.split(",") if p.strip())


def max_attempts() -> int:
    try:
        return max(1, int(settings.APEX_RETRY_ATTEMPTS))
    except (TypeError, ValueError):
        return 1


def is_permanent(hangup_cause: str | None) -> bool:
    """Would re-dialing this number ever work? A disconnected line is not a
    missed call, and retrying it burns dials, credits and the DID's reputation
    for a contact that can never answer."""
    cause = str(hangup_cause or "").upper()
    if not cause:
        return False  # unknown cause is not evidence of a dead number
    return any(skip in cause for skip in _skip_causes())


def next_attempt_at(
    *, attempt: int, hangup_cause: str | None, now: datetime | None = None
) -> datetime | None:
    """When to re-dial a contact whose attempt ``attempt`` just failed, or
    ``None`` to leave it terminal.

    ``None`` when retries are off, the attempt budget is spent, the cause is a
    dead end, or no offset is configured for this attempt number. ``attempt`` is
    the row's post-increment value — the claim bumps it at dial time, so a
    contact that has been called once arrives here with ``attempt == 1``.
    """
    limit = max_attempts()
    if limit <= 1:
        return None  # retries disabled — the shipped default
    if attempt >= limit:
        return None  # budget spent
    if is_permanent(hangup_cause):
        return None
    offsets = _offsets()
    idx = max(0, attempt - 1)
    if idx >= len(offsets):
        return None  # no offset configured for this retry → don't invent one
    base = now or datetime.now(timezone.utc)
    return base + timedelta(hours=offsets[idx])
