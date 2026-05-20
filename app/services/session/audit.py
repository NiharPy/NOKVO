"""Audit trail — chronological event log of policy actions during a call.

Owns exactly one key on ``NokvoOneToolRecord.data`` / flow_state:

* ``audit_trail`` — ``[{"at": iso, "event": str, "detail": Any?}]``

Other session modules append events here when they do something the
operator might want to reconstruct (a confirmation accepted, a retry
enqueued, an outcome recorded). They never read the trail back — this is
write-only from the system's perspective; reading is for humans.

Event names we use today:

* ``slot_captured``           — a slot was extracted
* ``name_confirmed``          — caller said yes to the read-back
* ``phone_confirmed``         — same, for phone
* ``email_confirmed``         — same, for email
* ``reference_number_confirmed`` — same, for IDs
* ``identity_challenged``     — verification gate fired
* ``proposed_slot_accepted``  — caller said yes to an offered slot
* ``tool_retry_enqueued``     — in-call retry failed, queued for later
* ``outcome_recorded``        — terminal outcome was written
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append(data: dict[str, Any], event: str, *, detail: Any = None) -> dict[str, Any]:
    """Add an event to the trail. Keep ``detail`` JSON-serialisable."""
    trail = data.get("audit_trail")
    if not isinstance(trail, list):
        trail = []
        data["audit_trail"] = trail
    entry: dict[str, Any] = {"at": _now(), "event": event}
    if detail is not None:
        entry["detail"] = detail
    trail.append(entry)
    return data


def events(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not data:
        return []
    trail = data.get("audit_trail")
    return trail if isinstance(trail, list) else []
