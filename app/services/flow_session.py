"""Backward-compat shim.

The original :mod:`flow_session` bundled four concerns into one module.
They now live in :mod:`app.services.session` with explicit boundaries:

* :mod:`app.services.session.confirmation` — handshake state
* :mod:`app.services.session.audit`        — chronological event log
* :mod:`app.services.session.retry`        — re-export of the retry queue
* :mod:`app.services.session.outcome`      — terminal outcome state

This file re-exports the original symbols so existing call sites
(``from app.services.flow_session import record_confirmation`` etc.) keep
working unchanged. New code should import from ``app.services.session``
directly so the boundary is visible at the call site.
"""

from __future__ import annotations

from typing import Any

from app.services.session.audit import append as _append_audit
from app.services.session.confirmation import (
    is_confirmed,
    is_identity_verified,
    mark_identity_verified,
    record_confirmation,
    stamp_proposed_slot_accepted as _stamp_proposed_slot_accepted,
)
from app.services.session.outcome import mark as _outcome_mark
from app.services.session.outcome import summary as outcome_summary


def append_audit_trail(data: dict[str, Any], event: str, *, detail: Any = None) -> dict[str, Any]:
    return _append_audit(data, event, detail=detail)


def mark_outcome(
    data: dict[str, Any],
    status: str,
    *,
    source: str = "system",
    notes: str | None = None,
) -> dict[str, Any]:
    return _outcome_mark(data, status, source=source, notes=notes)


def stamp_proposed_slot_accepted(
    data: dict[str, Any],
    *,
    slot_utc_iso: str,
    member_name: str | None = None,
) -> dict[str, Any]:
    """Compose the confirmation stamp with an audit-trail entry — the legacy
    helper did both, so we keep that joined behaviour here while the two
    underlying concerns sit in their own modules."""
    _stamp_proposed_slot_accepted(data, slot_utc_iso=slot_utc_iso, member_name=member_name)
    _append_audit(
        data,
        "proposed_slot_accepted",
        detail={"slot_utc": slot_utc_iso, "member_name": member_name},
    )
    return data


__all__ = [
    "append_audit_trail",
    "is_confirmed",
    "is_identity_verified",
    "mark_identity_verified",
    "mark_outcome",
    "outcome_summary",
    "record_confirmation",
    "stamp_proposed_slot_accepted",
]
