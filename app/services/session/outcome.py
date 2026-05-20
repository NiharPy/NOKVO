"""Outcome state for a record — the terminal "did the booking actually
happen?" signal.

Owns exactly one key on ``NokvoOneToolRecord.data``:

* ``outcome`` — ``{"status": str, "at": iso, "source": str, "notes": str | None}``

Status values come from :data:`app.services.agent_spec.OUTCOME_STATES` —
the canonical agent spec, not this module — so the spec is the single
source of truth and downstream consumers (API, dashboards, future LLM
self-evaluation) all check the same set.

The actual cross-cutting tracker (read by phone, write by record id)
lives in :mod:`app.services.outcome_tracker`. This module provides the
low-level mutators that operate on a single record's data dict, so they
can be called from policy code without an async DB session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.agent_spec import OUTCOME_STATES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark(
    data: dict[str, Any],
    status: str,
    *,
    source: str = "system",
    notes: str | None = None,
) -> dict[str, Any]:
    """Set the terminal outcome on a record's data dict.

    Raises :class:`ValueError` for unknown statuses — the closed set is
    defined by :data:`agent_spec.OUTCOME_STATES`, so any new outcome must
    be added there first (intentional friction to keep the spec
    authoritative).
    """
    if status not in OUTCOME_STATES.all_values():
        raise ValueError(f"Unknown outcome status: {status}")
    data["outcome"] = {
        "status": status,
        "at": _now(),
        "source": source,
        "notes": notes,
    }
    return data


def summary(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return None
    outcome = data.get("outcome")
    return outcome if isinstance(outcome, dict) else None
