"""Confirmation handshake state.

Owns these keys on ``NokvoOneToolRecord.data`` / flow_state:

* ``confirmations``           — ``{field: {"at": iso, "value": Any, "method": str}}``
* ``identity_verified``       — bool
* ``identity_verified_at``    — iso timestamp
* ``proposed_slot_accepted``  — ``{"at": iso, "slot_utc": str, "member_name": str | None}``

No other session module reads or writes these. The audit module writes a
side-event when a confirmation lands, but does not touch the keys here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_confirmation(
    data: dict[str, Any],
    field: str,
    value: Any,
    *,
    method: str = "voice_handshake",
) -> dict[str, Any]:
    """Stamp ``field`` as confirmed by the caller. ``value`` is the
    confirmed-as value (a "Niwas with a W" correction overwrites the
    original capture cleanly)."""
    confirmations = data.get("confirmations")
    if not isinstance(confirmations, dict):
        confirmations = {}
        data["confirmations"] = confirmations
    confirmations[field] = {"at": _now(), "value": value, "method": method}
    return data


def is_confirmed(data: dict[str, Any] | None, field: str) -> bool:
    if not data:
        return False
    confirmations = data.get("confirmations") or {}
    if not isinstance(confirmations, dict):
        return False
    return bool(confirmations.get(field))


def mark_identity_verified(data: dict[str, Any]) -> dict[str, Any]:
    data["identity_verified"] = True
    data["identity_verified_at"] = _now()
    return data


def is_identity_verified(data: dict[str, Any] | None) -> bool:
    if not data:
        return False
    return bool(data.get("identity_verified"))


def stamp_proposed_slot_accepted(
    data: dict[str, Any],
    *,
    slot_utc_iso: str,
    member_name: str | None = None,
) -> dict[str, Any]:
    data["proposed_slot_accepted"] = {
        "at": _now(),
        "slot_utc": slot_utc_iso,
        "member_name": member_name,
    }
    return data
